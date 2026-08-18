#!/usr/bin/env python3
"""
Cannibal screen — the one standing proactive channel (David sign-off 2026-08-13).

Cheap companies eating themselves: positive FCF, net cash, SHRINKING share
count, priced at a high FCF yield. Weekly, whole-market, pure code + one quote
pass. Screens PROPOSE, mechanism DISPOSES: survivors enter the scout funnel as
events, where the local analyst still has to answer "why is this cheap — who is
the wrong-price seller?" before the PM ever sees a row. A screen hit is never
a thesis (SOURCING.md unchanged).

How it stays cheap at market scale:
  1. SEC XBRL FRAMES: one request per concept returns EVERY filer's value
     (~5.7k CIKs for CFO). Six requests cover CFO, capex, cash, LT debt,
     shares now, shares a year ago. First-hand data, no per-name fetching.
  2. Fundamental filters run BEFORE any price is needed (FCF>0, net cash>=0,
     share count down >=2% y/y) -> ~few hundred survivors.
  3. One Finnhub quote pass over survivors only -> market cap, FCF yield,
     band-pass $100M-$10B (small/mid: where attention edge lives; mega-cap
     cannibals are efficiently priced), yield >= 8%.
  4. PERSISTENCE tracked: real cannibals stay on the screen for quarters
     (weeks_on_screen); one-week wonders are usually data artifacts.

Output: data/cannibal.json (top 15 + full survivor list); scout.py ingests the
top rows as 'cannibal-screen' events. Cron: Sundays 22:30 UTC.
CLI: cannibal.py run [--max-quotes 300]
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ENGINE = HERE.parent
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # SEC contact identity, config-driven
OUT = DATA / "cannibal.json"

MIN_CAP, MAX_CAP = 100e6, 10e9
MIN_YIELD = 0.08
MIN_SHRINK = 0.02   # share count down >=2% y/y
MAX_SHRINK = 0.30   # above this it's almost never buybacks (reverse split / tender)
                    # — those park in "extreme_shrink" for manual split-vs-tender review
# lenders/insurers/REITs: CFO is not FCF (loan collections, float, FFO world) —
# first live scan's "top" was CPSS (subprime lender, '138% yield') and an insurer.
EXCLUDE_INDUSTRIES = {"banking", "banks", "insurance", "financial services",
                      "real estate", "capital markets", "credit services",
                      "asset management", "mortgage", "reit", "thrifts"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read())
    time.sleep(0.25)
    return d


def frame(path):
    d = _get(f"https://data.sec.gov/api/xbrl/frames/{path}.json")
    return {row["cik"]: row["val"] for row in d.get("data", [])}


def latest_fy_frame(tag):
    """Most filers have FY2025 on file by Aug-2026; fall back merges CY2024 for
    off-calendar fiscal years so late filers aren't dropped."""
    cur = frame(f"us-gaap/{tag}/USD/CY2025")
    prev = frame(f"us-gaap/{tag}/USD/CY2024")
    return {**prev, **cur}



# ---------------------------------------------------------------- the watcher
# David, 2026-08-18: "cannibal should have local model monitoring."
#
# A whole-market screen fails QUIETLY. If a frames request half-returns, or the quote pass
# gets a stale price, the output is still a plausible-looking list of tickers — there is no
# traceback and no empty file to notice. So a local model looks at the RUN, not the names:
# is this shaped like the last one, and does anything here look like a data artifact rather
# than a business? It costs zero Claude tokens and takes one call.
#
# It never filters. It writes an opinion into cannibal.json alongside the hits, and a
# monitor that cannot run leaves an explicit "unavailable" rather than an implied all-clear.
MONITOR_PROMPT = """You are monitoring one run of a whole-market stock screen for DATA problems.
You are not judging whether these are good investments — only whether this run looks like it
worked. Criteria applied: {criteria}

This run:  {n_hits} hits from {survivors} fundamental survivors of {scanned} filers scanned.
Previous run ({prev_at}): {n_prev} hits.
New entrants this run: {new_list}
Dropped since last run: {dropped_list}

The new entrants, with their numbers:
{rows}

Answer ONLY with JSON:
{{"verdict": "<one sentence: does this run look sound, or what looks wrong>",
  "suspect": [{{"ticker": "<TK>", "why": "<what looks like a data artifact, not a business>"}}],
  "shape_change": "<one sentence on whether the hit count / survivor ratio moved abnormally vs the previous run, or 'normal'>"}}

What a data artifact looks like here: an FCF yield above ~80% (usually a market cap that
did not update, or a one-off asset sale counted as operating cash flow); a share-count drop
that is really a reverse split; a market cap that disagrees with price x shares; a company
whose 'net cash' comes from restricted or customer-held funds. If nothing looks wrong, return
an empty suspect list and say so. Never invent a number that is not in the rows above."""


def monitor(doc, new_entrants):
    """Local-model sanity check on the RUN. Zero Claude tokens; goes through the box's
    GPU queue like every other local call."""
    try:
        sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
        from localllm import ask_json
    except Exception as e:
        return {"ok": False, "verdict": "", "note": f"monitor unavailable: {type(e).__name__}: {e}"}
    rows = "\n".join(
        f"- {h['ticker']}: FCF yield {h.get('fcf_yield_pct')}%, share change "
        f"{h.get('share_shrink_pct')}%, net cash ${(h.get('net_cash') or 0) / 1e6:,.0f}M, "
        f"mkt cap ${(h.get('market_cap') or 0) / 1e6:,.0f}M, price {h.get('price')}, "
        f"runs on screen {h.get('runs_on_screen')}{', ' + h['fcf_note'] if h.get('fcf_note') else ''}"
        for h in new_entrants[:25]) or "(none — no new entrants this run)"
    try:
        out = ask_json(MONITOR_PROMPT.format(
            criteria=doc["criteria"], n_hits=doc["hits"], survivors=doc["fundamental_survivors"],
            scanned=doc["universe_scanned"], prev_at=doc.get("prev_ran_at") or "no prior run",
            n_prev=len(doc.get("dropped") or []) + doc["hits"] - len(doc.get("new_entrants") or []),
            new_list=", ".join(doc.get("new_entrants") or []) or "none",
            dropped_list=", ".join(doc.get("dropped") or []) or "none",
            rows=rows), num_predict=500, job="cannibal monitor")
    except Exception as e:
        return {"ok": False, "verdict": "", "note": f"monitor call failed: {type(e).__name__}: {e}"}
    if not isinstance(out, dict) or "verdict" not in out:
        return {"ok": False, "verdict": "", "note": "monitor returned no parseable verdict"}
    out["ok"] = True
    out["ran_at"] = doc["ran_at"]
    return out


def run(max_quotes=300):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"{now} cannibal: pulling frames…")
    cfo = latest_fy_frame("NetCashProvidedByUsedInOperatingActivities")
    capex = latest_fy_frame("PaymentsToAcquirePropertyPlantAndEquipment")
    cash = {**frame("us-gaap/CashAndCashEquivalentsAtCarryingValue/USD/CY2025Q4I"),
            **frame("us-gaap/CashAndCashEquivalentsAtCarryingValue/USD/CY2026Q1I")}
    # merge BOTH major debt tags, worst value wins — the LTD-noncurrent frame alone
    # missed DXC's multi-billion load and passed it as "net cash" (first-run lesson)
    debt = {}
    for tag in ("LongTermDebtNoncurrent", "LongTermDebt", "DebtCurrent"):
        for period in ("CY2025Q4I", "CY2026Q1I"):
            for cik, v in frame(f"us-gaap/{tag}/USD/{period}").items():
                debt[cik] = max(debt.get(cik, 0), v or 0) if tag != "DebtCurrent" \
                    else debt.get(cik, 0) + (v or 0)
    sh_now = {**frame("dei/EntityCommonStockSharesOutstanding/shares/CY2026Q1I"),
              **frame("dei/EntityCommonStockSharesOutstanding/shares/CY2026Q2I")}
    sh_ago = {**frame("dei/EntityCommonStockSharesOutstanding/shares/CY2025Q1I"),
              **frame("dei/EntityCommonStockSharesOutstanding/shares/CY2025Q2I")}
    tick = _get("https://www.sec.gov/files/company_tickers.json")
    tk_by_cik = {}
    for v in tick.values():   # first (primary) ticker per CIK wins
        tk_by_cik.setdefault(v["cik_str"], v["ticker"].upper())

    # ---- stage 1: fundamental filters, no prices ----
    survivors = []
    for cik, c in cfo.items():
        sn, sa = sh_now.get(cik), sh_ago.get(cik)
        if not c or c <= 0 or not sn or not sa or sn <= 0 or sa <= 0:
            continue
        shrink = 1 - sn / sa
        if shrink < MIN_SHRINK:
            continue
        cx = capex.get(cik)
        fcf = c - (cx or 0)
        if fcf <= 0:
            continue
        nc = (cash.get(cik) or 0) - (debt.get(cik) or 0)
        if nc < 0 or cash.get(cik) is None:
            continue
        tk = tk_by_cik.get(cik)
        if not tk or not tk.isalpha():
            continue
        survivors.append({"ticker": tk, "cik": cik, "fcf": fcf, "cfo": c,
                          "capex_known": cx is not None, "net_cash": nc,
                          "shares_now": sn, "share_shrink_pct": round(shrink * 100, 2)})
    print(f"fundamental pass: {len(survivors)} of {len(cfo):,} filers "
          f"(FCF>0, net cash>=0, shares -{MIN_SHRINK * 100:.0f}%+)")

    # ---- stage 2: one quote pass, band + yield ----
    try:
        key = json.loads((ENGINE / "config" / "keys.json").read_text()).get("finnhub", "")
    except Exception:
        key = ""
    icache_f = DATA / "industry_cache.json"
    icache = {}
    try:
        icache = json.loads(icache_f.read_text())
    except Exception:
        pass
    survivors.sort(key=lambda s: -min(s["share_shrink_pct"], MAX_SHRINK * 100))
    hits, extreme = [], []
    for s in survivors[:max_quotes]:
        try:
            with urllib.request.urlopen(
                    f"https://finnhub.io/api/v1/quote?symbol={s['ticker']}&token={key}", timeout=15) as r:
                px = (json.loads(r.read()) or {}).get("c")
        except Exception:
            px = None
        time.sleep(1.05)
        if not px:
            continue
        mc = px * s["shares_now"]
        if not (MIN_CAP <= mc <= MAX_CAP):
            continue
        fy = s["fcf"] / mc
        if fy < MIN_YIELD:
            continue
        tk = s["ticker"]
        if tk not in icache:
            try:
                with urllib.request.urlopen(
                        f"https://finnhub.io/api/v1/stock/profile2?symbol={tk}&token={key}", timeout=15) as r:
                    icache[tk] = (json.loads(r.read()) or {}).get("finnhubIndustry") or "unknown"
            except Exception:
                icache[tk] = "unknown"
            time.sleep(1.05)
        ind = str(icache.get(tk, "")).lower()
        if any(x in ind for x in EXCLUDE_INDUSTRIES):
            continue
        row = {**s, "price": px, "market_cap": round(mc), "industry": icache.get(tk),
               "fcf_yield_pct": round(fy * 100, 2),
               "fcf_note": "" if s["capex_known"] else "capex tag missing — FCF = CFO upper bound",
               "composite": round(fy * 100 + min(s["share_shrink_pct"], MAX_SHRINK * 100), 2)}
        if s["share_shrink_pct"] > MAX_SHRINK * 100:
            extreme.append(row)   # split-vs-tender: real tenders belong in the funnel, splits don't
        else:
            hits.append(row)
    hits.sort(key=lambda h: -h["composite"])
    icache_f.write_text(json.dumps(icache, indent=1))

    # ---- persistence: real cannibals persist; one-run wonders are usually data artifacts ----
    # DAILY since 2026-08-18 (David: "why can't cannibal run daily?"). It always could — the
    # whole run is ~4 minutes and costs no Claude tokens; the binding cost is ~233 Finnhub
    # quotes, well inside budget. Weekly was an unexamined default, and it cost us latency:
    # a name that newly qualifies is a dated event, and finding it six days late is six days
    # of the move given away. Daily also makes the DELTA meaningful, which is the real signal
    # here — not the standing list, but who joined it and who fell off.
    prev_doc, prev = {}, {}
    try:
        prev_doc = json.loads(OUT.read_text())
        prev = {h["ticker"]: h for h in prev_doc.get("all_hits", [])}
    except Exception:
        pass
    for h in hits:
        p0 = prev.get(h["ticker"], {})
        # carry the old weekly counter forward so history is not lost in the cadence change
        h["runs_on_screen"] = (p0.get("runs_on_screen") or p0.get("weeks_on_screen") or 0) + 1
        h["first_seen"] = p0.get("first_seen") or now[:10]
    cur_t = {h["ticker"] for h in hits}
    prev_t = set(prev)
    new_entrants = [h for h in hits if h["ticker"] not in prev_t]
    dropped = sorted(prev_t - cur_t)
    top = hits[:15]

    doc = {
        "ran_at": now, "criteria": f"FCF>0 · net cash>=0 · shares -{MIN_SHRINK * 100:.0f}%+ y/y · "
        f"cap ${MIN_CAP / 1e6:.0f}M-${MAX_CAP / 1e9:.0f}B · FCF yield >={MIN_YIELD * 100:.0f}%",
        "universe_scanned": len(cfo), "fundamental_survivors": len(survivors),
        "quoted": min(len(survivors), max_quotes), "hits": len(hits),
        "prev_ran_at": prev_doc.get("ran_at"),
        "new_entrants": [h["ticker"] for h in new_entrants],
        "dropped": dropped,
        "top": top, "all_hits": hits,
        "extreme_shrink": extreme[:15]}
    doc["monitor"] = monitor(doc, new_entrants)
    OUT.write_text(json.dumps(doc, indent=1))
    print(f"{now} cannibal: {len(hits)} hits · +{len(new_entrants)} new · -{len(dropped)} dropped · "
          f"top: {[(h['ticker'], h['fcf_yield_pct'], h['share_shrink_pct']) for h in top[:6]]}")
    if doc["monitor"].get("verdict"):
        print(f"  monitor: {doc['monitor']['verdict']}")
    return top


if __name__ == "__main__":
    mq = int(sys.argv[sys.argv.index("--max-quotes") + 1]) if "--max-quotes" in sys.argv else 300
    if sys.argv[1:2] == ["run"] or not sys.argv[1:]:
        run(mq)
    else:
        sys.exit("usage: cannibal.py run [--max-quotes 300]")
