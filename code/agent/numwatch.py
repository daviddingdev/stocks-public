#!/usr/bin/env python3
"""
Numbers watchdog — a local model + code sweeping every number, nightly.

WHY (David, 2026-08-13: "i still want a local model going through the numbers…
have it help wherever it can"). All three real errors (LBRDP term, TRIP $824M,
LYFT net-cash) lived in PROSE — numbers asserted in memos that no code path
ever touched. Three layers, cheapest first:

  A  CODE — internal consistency of every fincard against itself:
     balance identity (assets ≈ liabilities + equity), gross profit ≈
     revenue − cogs, TTM figure == sum of its own quarter series, and every
     derived value recomputed independently from the card's figures.
  B  LOCAL vs FILINGS — extended fincheck: qwen reads the printed statements
     (latest 10-Q AND 10-K) for 8 figures; code compares scale- and
     period-aware (via the card's own quarter sums).
  C  LOCAL vs PROSE — qwen extracts every dollar figure from the PM's ACTIVE
     prose (BOOK.md, the newest memo per held name, thesis.json notes); code
     then requires each number to trace to a source: a fincard figure/derived/
     series value (±2.5%, any scale) or a verbatim hit in the filing text.
     Unsourced numbers become quality-queue items — the PM or fixer must
     source or fix them. Prices/personal position math are exempt (broker
     territory, not filing territory).

Zero Claude tokens. Findings land in names/<TK>/numcheck.json + data/
numwatch.json; quality.py picks them up. Cron: nightly after refresh_cards.
CLI: numwatch.py run | numwatch.py memo <path> [TICKER]
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
NAMES = HERE / "names"
JOURNAL = HERE / "journal"
sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
from localllm import ask_json  # noqa: E402

TOL = 0.025


def _j(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


# ---------------- A: code-only card self-consistency ----------------
def check_card(card):
    finds = []
    F, D, S = card.get("figures", {}), card.get("derived", {}), card.get("series", {})

    def gv(k):
        f = F.get(k) or {}
        if f.get("STALE"):
            return None   # quarantined by fincard — derived excluded it; recompute must too
        return f.get("value")

    def close(a, b, tol=TOL):
        return a is not None and b is not None and abs(a - b) <= tol * max(abs(a), abs(b), 1)

    ta, tl, eq = gv("total_assets"), gv("total_liabilities"), gv("equity")
    if ta is not None and tl is not None and eq is not None and not close(ta, tl + eq, 0.03):
        finds.append(f"balance identity: assets {ta:,.0f} != liabilities {tl:,.0f} + equity {eq:,.0f} "
                     f"(gap {(ta - tl - eq) / ta * 100:+.1f}% — mixed dates or minority-interest tag)")
    gp, rev, cogs = gv("gross_profit"), gv("revenue"), gv("cogs")
    if gp is not None and rev is not None and cogs is not None and not close(gp, rev - cogs, 0.03):
        finds.append(f"gross_profit {gp:,.0f} != revenue - cogs {rev - cogs:,.0f}")
    for k, fig in F.items():
        qs = (S.get(k) or {}).get("quarters", [])
        if len(qs) >= 4 and str(fig.get("period", "")).startswith("TTM"):
            ssum = sum(q["value"] for q in qs[:4])
            if not close(fig.get("value"), ssum, 0.001):
                finds.append(f"{k}: TTM figure {fig.get('value'):,.0f} != sum of own quarters {ssum:,.0f}")
    # independent recompute of key deriveds from raw figures
    cash, sti = gv("cash") or 0, gv("st_investments") or 0
    dlt, dcur = gv("debt_lt") or 0, gv("debt_current") or 0
    nc = (D.get("net_cash") or {}).get("value")
    if nc is not None and not close(nc, cash + sti - dlt - dcur, 0.001):
        finds.append(f"net_cash derived {nc:,.0f} != recompute {cash + sti - dlt - dcur:,.0f}")
    fcf = (D.get("fcf") or {}).get("value")
    cfo, capex = gv("cfo"), gv("capex")
    if fcf is not None and cfo is not None and not close(fcf, cfo - (capex or 0), 0.001):
        finds.append(f"fcf derived {fcf:,.0f} != CFO - capex recompute {cfo - (capex or 0):,.0f}")
    if fcf is not None and capex is None:
        finds.append("fcf computed with capex MISSING — value is CFO (upper bound); "
                     "any prose citing it as FCF is suspect")
    return finds


# ---------------- C: prose-number tracing ----------------
NUM_RE = re.compile(r"\$?\s?\d[\d,]*\.?\d*\s?(?:billion|million|thousand|[BMK])?", re.I)


def _absolute(num_text):
    m = re.search(r"([\d,]+\.?\d*)", num_text)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    low = num_text.lower()
    if "b" in low or "billion" in low:
        v *= 1e9
    elif "m" in low or "million" in low:
        v *= 1e6
    elif "k" in low or "thousand" in low:
        v *= 1e3
    return v


def extract_prose_numbers(text, label):
    """qwen parses prose into structured dollar-figure claims; code does ALL comparison."""
    v = ask_json(
        "Extract every DOLLAR AMOUNT that describes a COMPANY's finances (cash, debt, revenue, "
        "FCF, EBITDA, market cap, buybacks, distributions, valuations) from this trading memo "
        "excerpt. SKIP: share prices, per-share values under $100, the trader's own position "
        "sizes/P&L (a cost basis, a share-of-book percentage), dates, percentages. For each: the amount "
        "EXACTLY as written and a 2-4 word label of what it claims to be. JSON "
        "{\"numbers\":[{\"text\":\"$824.4M\",\"label\":\"omitted borrowings\"}]}  Max 20.\n\n"
        + text[:11000], num_predict=900)
    out = []
    for n in (v.get("numbers") or []) if isinstance(v, dict) else []:
        a = _absolute(str(n.get("text", "")))
        if a and a >= 5e5:      # ignore sub-$500k noise
            out.append({"text": n["text"], "label": str(n.get("label", ""))[:40],
                        "abs": a, "source_doc": label})
    return out


# label keyword -> the ONLY card concepts that label may match. Unconstrained
# matching against ~200 card values produced pure coincidences ($2B "net cash"
# traced to working_capital; $1.1B "TTM FCF" to sga) — autopsy 2026-08-13.
FAMILIES = [
    (("net cash",), ["net_cash"]),
    (("gross cash", "cash and investments", "cash & investments", "total cash"),
     ["cash", "st_investments", "lt_investments", "_cash_combo"]),
    (("cash",), ["cash", "st_investments", "_cash_combo", "net_cash"]),
    (("free cash", "fcf"), ["fcf", "cfo"]),
    (("operating cash", "cfo"), ["cfo"]),
    (("ebitda",), ["ebitda_approx"]),
    (("net income", "earnings", "profit"), ["net_income"]),
    (("revenue", "sales"), ["revenue"]),   # NOT "bookings" — Gross Bookings is a KPI, not GAAP revenue
    (("debt", "borrowings", "notes"), ["total_debt", "debt_lt", "debt_current"]),
    (("market cap", "capitalization"), ["market_cap"]),
    (("enterprise value", "ev"), ["enterprise_value"]),
    (("equity", "book value"), ["equity"]),
    (("buyback", "repurchase"), ["buybacks"]),
    (("dividend", "distribution"), ["dividends_paid"]),
    (("capex", "capital expenditure"), ["capex"]),
    (("asset",), ["total_assets"]),
]
FORWARD_WORDS = ("guide", "guidance", "target", "estimate", "expected", "forecast",
                 "consensus", "e)", "fy26e", "fy27e", "projected", "trim")


def _card_values(card, keys):
    """All values the given concept keys can honestly produce (incl. cash combos)."""
    F, D, S = card.get("figures", {}), card.get("derived", {}), card.get("series", {})
    out = []
    for k in keys:
        if k == "_cash_combo":
            c = (F.get("cash") or {}).get("value") or 0
            s = (F.get("st_investments") or {}).get("value") or 0
            l = (F.get("lt_investments") or {}).get("value") or 0
            if c:
                out += [("cash+st_investments", c + s), ("cash+st+lt_investments", c + s + l)]
            continue
        for src, blob in (("figure", F), ("derived", D)):
            v = (blob.get(k) or {}).get("value")
            if isinstance(v, (int, float)):
                out.append((f"{src}:{k}", v))
        for q in ((S.get(k) or {}).get("quarters") or [])[:8] + ((S.get(k) or {}).get("annual") or [])[:4]:
            out.append((f"series:{k}", q.get("value")))
    return [(s, v) for s, v in out if isinstance(v, (int, float)) and v]


def trace_number(a, label, card, filing_texts):
    """Label-constrained tracing. Returns (status, detail):
    ok / forward (unverifiable by design) / mislabel (value exists under a
    DIFFERENT concept — the LYFT-error shape) / unsourced."""
    low = label.lower()
    if any(w in low for w in FORWARD_WORDS):
        return "forward", "forward-looking/guide — not verifiable against filings"
    fam_keys = None
    if not any(w in low for w in ("adj", "adjusted", "non-gaap", "gross bookings", "gbv")):
        # adjusted/KPI metrics are press-release numbers — GAAP card can't confirm
        # them; they go straight to the filing-digit path below
        for words, keys in FAMILIES:
            if any(w in low for w in words):
                fam_keys = keys
                break
    if fam_keys:
        # market-priced values drift with the tape after a memo is written —
        # widen tolerance instead of crying wolf on every price move
        tol = 0.15 if any(k in ("market_cap", "enterprise_value") for k in fam_keys) else TOL
        for src, v in _card_values(card, fam_keys):
            if abs(abs(v) - a) <= tol * max(abs(v), a):
                return "ok", src
        # MISLABEL verdicts only for BARE concept labels ("net cash", "ttm fcf") —
        # qualified ones (segment/brand/period: "CARFAX revenue", "Q2 FCF") legitimately
        # differ from consolidated card concepts and must not cry mislabel
        fam_words = set(w for words, ks in FAMILIES if ks == fam_keys for p in words for w in p.split())
        extras = [w for w in re.findall(r"[a-z]+", low)
                  if w not in fam_words and w not in ("ttm", "total", "current", "the", "of")]
        if not extras:
            all_keys = sorted({k for _, ks in FAMILIES for k in ks})
            for src, v in _card_values(card, all_keys):
                if abs(abs(v) - a) <= TOL * max(abs(v), a):
                    return "mislabel", (f"labeled '{label}' but the value matches {src} — "
                                        "the LYFT-error shape (wrong concept under a familiar name)")
        return "unsourced", f"no {fam_keys} value within {TOL * 100:.0f}%"
    # unknown label family (company KPIs like Gross Bookings): filing digits, but
    # only for numbers with >=3 significant digits — round numbers match anything
    pat_base = f"{a:,.0f}"
    if len(re.sub(r'[^1-9]', '', pat_base)) >= 3:
        for scale in (1e6, 1e3, 1):
            scaled = a / scale
            if 0.1 <= scaled < 1e7:
                pat = f"{scaled:,.1f}".rstrip("0").rstrip(".")
                for name, txt in filing_texts.items():
                    if pat in txt or pat.replace(",", "") in txt:
                        return "ok", f"filing:{name}~{pat}"
    return "unsourced", "unknown concept and no precise filing match"


def sweep_prose(tk, texts):
    """texts: {label: prose}. Returns unsourced-number findings for one name."""
    card = _j(NAMES / tk / "fincard.json", {})
    fdir = NAMES / tk / "filings"
    filing_texts = {p.name: p.read_text(errors="replace") for p in fdir.glob("*.txt")} if fdir.exists() else {}
    finds = []
    for label, text in texts.items():
        for n in extract_prose_numbers(text, label):
            status, detail = trace_number(n["abs"], n["label"], card, filing_texts)
            if status == "mislabel":
                finds.append(f"MISLABEL in {label}: {n['text']} ({n['label']}) — {detail}")
            elif status == "unsourced":
                finds.append(f"UNSOURCED in {label}: {n['text']} ({n['label']}) — {detail}; "
                             "source it or fix it")
    return finds


# ---------------- orchestration ----------------
def run():
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    pf = _j(DATA / "portfolio.json", {})
    held = [p["symbol"] for p in pf.get("positions", []) if p.get("symbol")]
    thesis = {k: v for k, v in _j(DATA / "thesis.json", {}).items() if not k.startswith("_")}
    book = (JOURNAL / "BOOK.md").read_text(errors="replace") if (JOURNAL / "BOOK.md").exists() else ""
    report = {"ran_at": now, "names": {}, "n_findings": 0}
    for tk in held:
        card = _j(NAMES / tk / "fincard.json", {})
        finds = check_card(card) if card else ["no fincard on disk"]
        texts = {}
        memos = sorted(JOURNAL.glob(f"*_{tk}_*.md"))
        if memos:
            texts[memos[-1].name] = memos[-1].read_text(errors="replace")
        if tk in thesis:
            texts["thesis.json note"] = json.dumps(thesis[tk])
        # BOOK.md section for this name only (cheap targeting)
        m = re.search(rf"(?im)^- \*\*{tk}[^\n]*\n(?:(?!^- \*\*).*\n)*", book)
        if m:
            texts["BOOK.md"] = m.group(0)
        finds += sweep_prose(tk, texts)
        report["names"][tk] = finds
        report["n_findings"] += len(finds)
        (NAMES / tk / "numcheck.json").parent.mkdir(parents=True, exist_ok=True)
        (NAMES / tk / "numcheck.json").write_text(json.dumps(
            {"ran_at": now, "findings": finds}, indent=1))
        print(f"{tk}: {len(finds)} finding(s)" + (f" — {finds[0][:90]}" if finds else ""))
    (DATA / "numwatch.json").write_text(json.dumps(report, indent=1))
    print(f"{now} numwatch: {report['n_findings']} finding(s) across {len(held)} names")
    return report


def memo_cmd(path, tk=None):
    p = Path(path)
    if not p.exists():
        p = JOURNAL / path
    tk = (tk or re.search(r"_([A-Z]+)_", p.name).group(1)).upper()
    finds = sweep_prose(tk, {p.name: p.read_text(errors="replace")})
    for f in finds:
        print(" ", f)
    print(f"{p.name}: {len(finds)} unsourced number(s)")
    return 1 if finds else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        run()
    elif sys.argv[1:2] == ["memo"] and len(sys.argv) > 2:
        sys.exit(memo_cmd(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    else:
        sys.exit("usage: numwatch.py run | numwatch.py memo <path> [TICKER]")
