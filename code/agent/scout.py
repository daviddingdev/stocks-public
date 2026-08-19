#!/usr/bin/env python3
"""
Scout funnel — origination, staged cheap-first (David's design sign-off 2026-08-13).

The verification stack got world-class while sourcing stayed "the PM skims raw
rows." This is the fix: code collects and computes, the LOCAL model asks the
SOURCING doctrine's questions, Opus judges only the ranked top of the funnel.

  Stage 0  COLLECT (code)   events from feed.json/managers.json: subject-resolved
                            13Ds, spins, delistings, 13F new stakes, universe
                            insider clusters. Deduped by event id, remembered
                            forever (no re-triaging the same 13D five times —
                            the board_memory lesson applied to sourcing).
  Stage 1  PRE-TRIAGE (local, event-only): plausibility 0-10. Junk dies free.
  Stage 2  ENRICH (code):   plausibility >=5 -> fincard built (codified numbers).
  Stage 3  TRIAGE (local, event + numbers): mechanism-fit 0-10 against the
                            doctrine's channels + a 4-sentence sketch + what the
                            variant perception would have to be + red flags.
  Stage 4  PM (Opus):       reads the ranked queue top in its daily session;
                            every pass/decline recorded on the candidate.

data/candidates.json lifecycle: new -> pre_triaged -> triaged -> pm_reviewed ->
underwriting | passed | dropped.  Zero Claude tokens anywhere in this file.
CLI: scout.py run | scout.py list
"""
import datetime as dt
import json
import os
import time
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
NAMES = HERE / "names"
sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
from localllm import ask_json  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # noqa: E402  — SEC contact identity, config-driven

CAND = DATA / "candidates.json"

CHANNELS = ("spinoff-flush, forced-selling (index/fund mandates, delisting, margin), "
            "activist-coattail (13D with real economics), insider-cluster-buying, "
            "busted-growth (story break priced as terminal), structural-discount "
            "(CEF/holdco arithmetic, tender/reorg with dated terms), event-overreaction")

PRE_PROMPT = f"""You are the junior sourcing analyst for a mechanism-driven value fund.
Doctrine: a candidate qualifies only if we can say WHO is selling (or mispricing) for
NON-VALUE reasons. Channels: {CHANNELS}.
Rate this raw event's PLAUSIBILITY as a mechanism lead, 0-10:
0-2 = noise (SPAC mechanics, routine institutional filing, micro-cap pump shapes,
technical/catch-up filings); 3-4 = thin; 5-7 = worth pulling numbers; 8-10 = textbook setup.
Judge ONLY from the event given. JSON {{"plausible": <int>, "why": "<max 20 words>",
"channel": "<one channel name or none>"}}"""

TRIAGE_PROMPT = f"""You are the junior sourcing analyst for a mechanism-driven value fund.
Channels: {CHANNELS}.
Given the EVENT and the code-computed FINANCIAL CARD SUMMARY, score MECHANISM FIT 0-10
(not cheapness — a stated non-value seller + a dated path matters more than a low multiple).
Answer the doctrine's questions. JSON:
{{"score": <int>, "channel": "<channel>",
 "sketch": "<=4 sentences: who is selling for non-value reasons, what the market is missing,
            the dated catalyst if any, and what kills it>",
 "variant_needed": "<one sentence: what we'd have to believe that the market doesn't>",
 "red_flags": "<comma list or none>"}}"""


def _j(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


_CORP_SUFFIX = {"inc", "corp", "corporation", "llc", "lp", "llp", "ltd", "co",
                "company", "holdings", "holding", "group", "the", "plc"}


def _name_tokens(name):
    norm = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    return {t for t in norm.split() if t not in _CORP_SUFFIX}


def _self_filed_13d(filer, subject):
    """Issuer self-filed / SPAC-sponsor 13Ds: every meaningful filer-name token
    also appears in the subject name (2026-08-19, fixer-002 — the PM hand-closed
    67 of these as mechanically identifiable noise: filer name token-subset of
    subject name, e.g. "Catalyst Acquisition Corp." filing on itself)."""
    if not filer or not subject or subject == "?":
        return False
    ftoks = _name_tokens(filer)
    return bool(ftoks) and ftoks <= _name_tokens(subject)


def _events():
    """Stage 0: normalized events with stable ids from the feeds."""
    feed = _j(DATA / "feed.json", {})
    ev = []
    sit = feed.get("situations") or {}
    for r in (sit.get("sc13d") or []):
        if _self_filed_13d(r.get("company"), r.get("subject")):
            continue
        tk = r.get("subject_ticker")
        d = r.get("date", "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        # The id must NOT contain the ticker. It used to be f"13d:{tk or cik}:{acc}", so the
        # moment the subject resolver filled a ticker in, the SAME filing was collected again
        # under a new id — leaving its ticker-less twin in the funnel forever, unscoreable
        # ("13D filed but lacks issuer") and permanently stuck at pre_triaged. 36 of 40 13D
        # accessions were sitting in the queue twice. The accession is the stable identity.
        ev.append({"id": f"13d:{r['url'].rsplit('/', 1)[-1]}",
                   "kind": "13D", "ticker": tk, "date": d,
                   "detail": f"SCHEDULE 13D by {r.get('company', '?')} on "
                             f"{r.get('subject', tk or '?')}", "url": r.get("url")})
    for r in (sit.get("spins") or []):
        ev.append({"id": f"spin:{r.get('cik')}:{r.get('date')}", "kind": "spin-registration",
                   "ticker": r.get("ticker"), "date": r.get("date"),
                   "detail": f"Form 10-12B: {r.get('company', '?')}", "url": r.get("url")})
    for g in (feed.get("managers") or {}).get("managers", []):
        for n in (g.get("new") or []):
            if n.get("putCall"):
                continue
            ev.append({"id": f"13f:{g['cik']}:{n.get('cusip')}:{g.get('period')}",
                       # Same disease as the 13D id bug, other channel: the ticker was hardcoded
                       # None and only resolved at ENRICH time — which a ticker-less row can
                       # never reach, because the gate scores it 3 for "lacks issuer". Resolve
                       # at collection, where it costs one dict lookup and unblocks the row.
                       "kind": "13F-new-stake", "ticker": _ticker_from_issuer(n.get("issuer")),
                       "date": g.get("filed"),
                       "issuer": n.get("issuer"),
                       "detail": f"{g['name']} NEW {n.get('issuer')} — {n.get('pct_port')}% of "
                                 f"their book (Q ended {g.get('period')}; 45d stale)"})
    can = _j(DATA / "cannibal.json", {})
    for h in can.get("top", []):
        ev.append({"id": f"can:{h['ticker']}:{str(can.get('ran_at', ''))[:10]}",
                   "kind": "cannibal-screen", "ticker": h["ticker"], "date": str(can.get("ran_at", ""))[:10],
                   "detail": (f"Cannibal screen: FCF yield {h['fcf_yield_pct']}%, shares "
                              f"-{h['share_shrink_pct']}% y/y, net cash ${h['net_cash'] / 1e6:,.0f}M, "
                              f"cap ${h['market_cap'] / 1e6:,.0f}M, {h.get('runs_on_screen') or h.get('weeks_on_screen') or 1} run(s) on screen"
                              + (f" · {h['fcf_note']}" if h.get("fcf_note") else "")
                              + " — a screen hit is a QUESTION: why is it cheap, who is the wrong-price seller?")})
    cutoff = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    pf_held = {p.get("symbol") for p in _j(DATA / "portfolio.json", {}).get("positions", [])}
    for tk, fils in (feed.get("filings") or {}).items():
        c = sum(1 for f in fils or [] if f.get("form") == "4" and f.get("date", "") >= cutoff)
        if c >= 3 and tk not in pf_held:
            ev.append({"id": f"f4x:{tk}:{cutoff}", "kind": "insider-cluster", "ticker": tk,
                       "date": cutoff, "detail": f"{c} Form 4s on {tk} within 2 days "
                       "(buys or routine vesting? — check the filings)"})
    return ev


_SEC_MAP = {}

def _sec_map():
    """The SEC name->ticker map, fetched once per process and cached on disk for a week.
    This used to be re-downloaded on EVERY call, which was tolerable when it ran a handful
    of times at enrich; it is now called for every 13F stake at collection."""
    global _SEC_MAP
    if _SEC_MAP:
        return _SEC_MAP
    cache = DATA / "sec_company_tickers.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        _SEC_MAP = _j(cache, {})
        if _SEC_MAP:
            return _SEC_MAP
    try:
        import urllib.request
        req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json",
                                     headers=UA)
        _SEC_MAP = json.loads(urllib.request.urlopen(req, timeout=30).read())
        cache.write_text(json.dumps(_SEC_MAP))
    except Exception:
        _SEC_MAP = _j(cache, {})
    return _SEC_MAP


def _ticker_from_issuer(issuer):
    """13F rows carry issuer NAMES; best-effort exact-ish match against the SEC map."""
    if not issuer:
        return None
    data = _sec_map()
    if not data:
        return None
    want = re.sub(r"[^a-z0-9]", "", issuer.lower())[:14]
    for v in data.values():
        have = re.sub(r"[^a-z0-9]", "", v["title"].lower())[:14]
        if want and want == have:
            return v["ticker"].upper()
    return None


def _fincard_summary(tk):
    card = _j(NAMES / tk / "fincard.json", {})
    if not card:
        return None, None
    D = card.get("derived", {})
    keys = ("market_cap", "enterprise_value", "net_cash", "fcf", "ev_over_fcf",
            "fcf_yield_pct", "revenue_growth_pct", "pe", "price_over_book",
            "share_count_change_pct", "debt_over_ebitda")
    lines = [f"{k}={D[k]['value']:,.2f}" for k in keys if k in D]
    flags = card.get("flags", [])
    txt = "; ".join(lines) + ("; FLAGS: " + " | ".join(f[:60] for f in flags[:3]) if flags else "")
    return txt, card


def run(max_pre=25, max_triage=8):
    q = _j(CAND, {"_doc": "scout funnel — see scout.py", "items": {}})
    items = q.get("items", {})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    evs = _events()
    for e in evs:  # late-resolving fields (13D subject tickers) land on the existing row
        cur = items.get(e["id"])
        if cur and not cur.get("ticker") and e.get("ticker"):
            cur["ticker"], cur["detail"] = e["ticker"], e.get("detail") or cur.get("detail")
            if cur.get("status") == "pre_triaged":
                cur["status"] = "new"   # it can be judged on its merits now — re-score it
    new = [e for e in evs if e["id"] not in items]
    n_pre = n_tri = 0
    for e in new:
        items[e["id"]] = {**e, "status": "new", "first_seen": now}
    # Stage 1: pre-triage newest-first, capped per run
    todo = [i for i in items.values() if i["status"] == "new"]
    todo.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    for it in todo[:max_pre]:
        v = ask_json(PRE_PROMPT + "\n\nEVENT: " + json.dumps(
            {k: it.get(k) for k in ("kind", "ticker", "issuer", "date", "detail")}),
            num_predict=200)
        it["pre"] = {"plausible": int(v.get("plausible", 0)) if str(v.get("plausible", "")).isdigit() else 0,
                     "why": str(v.get("why", ""))[:90], "channel": str(v.get("channel", ""))[:40]} \
            if isinstance(v, dict) else {"plausible": 0, "why": "triage failed"}
        # THE GATE WAS CIRCULAR (David, 2026-08-14: "why isn't it buying with so much cash").
        # Stage 1 is told to "judge ONLY from the event given" — and a 13D event line carries
        # no economics, so an honest junior analyst scores it 3-4 and it dies here forever.
        # Stage 2 exists precisely to supply the numbers stage 1 lacked, but it sat behind a
        # score stage 1 could not reach WITHOUT those numbers. Across 150 candidates the max
        # score ever achieved was 6 and only 3 were ever fully triaged. Proof it was
        # miscalibrated rather than strict: ETD — the one lead the PM promoted to underwriting
        # on 2026-08-14 after reading the actual 13D (six-person operator slate, explicit sale
        # language) — scored 4 and was blocked. The PM found it by going around the funnel.
        # So: an identified issuer the model merely could not read from the event text still
        # earns its numbers. This changes what the PM SEES, never what it may buy — a triage
        # score is a LEAD, and the full evidence gate downstream is unchanged.
        pl = it["pre"]["plausible"]
        it["status"] = "enriching" if (pl >= 5 or pl >= 4 or (pl >= 3 and it.get("ticker"))) else "pre_triaged"
        n_pre += 1
    # Re-check the standing backlog against the CURRENT gate. Scores are already stored,
    # so this costs no model call — and without it a gate change only ever applies to
    # events that arrive after it, leaving everything already collected dead forever.
    for it in items.values():
        if it["status"] == "pre_triaged":
            pl = (it.get("pre") or {}).get("plausible") or 0
            if pl >= 4 or (pl >= 3 and it.get("ticker")):
                it["status"] = "enriching"

    # Stage 2+3: enrich + full triage for the plausible, capped per run
    for it in [i for i in items.values() if i["status"] == "enriching"][:max_triage]:
        tk = it.get("ticker")
        if not tk and it.get("issuer"):
            tk = _ticker_from_issuer(it["issuer"])
            it["ticker"] = tk
        if not tk:
            it["status"] = "no_ticker"
            continue
        summary, card = _fincard_summary(tk)
        if not summary:
            try:
                sys.path.insert(0, str(HERE.parent / "valuation"))
                import fincard
                card = fincard.build(tk)
                (NAMES / tk).mkdir(parents=True, exist_ok=True)
                (NAMES / tk / "fincard.json").write_text(json.dumps(card, indent=1))
                summary, _ = _fincard_summary(tk)
            except Exception as ex:
                it["status"] = "enrich_failed"
                it["error"] = str(ex)[:120]
                continue
        v = ask_json(TRIAGE_PROMPT + "\n\nEVENT: " + json.dumps(
            {k: it.get(k) for k in ("kind", "ticker", "issuer", "date", "detail")})
            + "\n\nFINANCIAL CARD SUMMARY: " + (summary or "unavailable"), num_predict=600)
        if isinstance(v, dict) and str(v.get("score", "")).lstrip("-").isdigit():
            it["triage"] = {"score": int(v["score"]), "channel": str(v.get("channel", ""))[:40],
                            "sketch": str(v.get("sketch", ""))[:500],
                            "variant_needed": str(v.get("variant_needed", ""))[:200],
                            "red_flags": str(v.get("red_flags", ""))[:200], "at": now}
            it["status"] = "triaged"
        else:
            it["status"] = "triage_failed"
        n_tri += 1
    q["items"] = items
    q["scanned_at"] = now
    CAND.write_text(json.dumps(q, indent=1))
    top = sorted((i for i in items.values() if i["status"] == "triaged"),
                 key=lambda x: -x["triage"]["score"])[:5]
    print(f"{now} scout: {len(new)} new events · {n_pre} pre-triaged · {n_tri} triaged · "
          f"queue top: {[(t.get('ticker'), t['triage']['score']) for t in top]}")


def list_items():
    q = _j(CAND, {})
    rows = sorted(q.get("items", {}).values(),
                  key=lambda x: -(x.get("triage", {}).get("score") or x.get("pre", {}).get("plausible", 0) or 0))
    for i in rows[:25]:
        t = i.get("triage") or {}
        p = i.get("pre") or {}
        score = t.get("score", f"pre:{p.get('plausible', '?')}")
        print(f"[{i['status']:12s}] {score!s:>6} {str(i.get('ticker') or '—'):6s} {i['kind']:16s} "
              f"{(t.get('sketch') or p.get('why') or i.get('detail', ''))[:90]}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        run()
    elif sys.argv[1:2] == ["list"]:
        list_items()
    else:
        sys.exit("usage: scout.py run | scout.py list")
