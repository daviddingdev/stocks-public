#!/usr/bin/env python3
"""
Evidence-pack builder — CODE-ONLY pre-fetch for deep-research teardowns.

Runs BEFORE the Opus session (research_prompt step 0). Builds
<Company>-<TK>/research/_evidence/ so five pillar agents stop spending turns
re-fetching the same primary sources:

  manifest.json   what's in the pack + SEC identity
  facts.json      XBRL time series (revenue, income, FCF parts, cash, debt,
                  shares, equity) straight from companyfacts — the model's
                  numbers come from here instead of N curl round-trips
  filings/*.txt   text-extracted latest 10-K, 10-Qs, DEF 14A, recent 8-Ks
  sections.json   regex-located Item offsets per filing (10-K/10-Q items)
  INDEX.md        navigation map — code skeleton + OPTIONAL local-model notes
                  (navindex.py); pointers only, never a substitute for raw text

Accuracy doctrine: this changes WHERE agents read from, never WHAT they may
read — raw EDGAR stays fully available to them.
CLI: evidence.py TICKER [--skip-local]
"""
import datetime as dt
import html as htmllib
import json
import re
import sys
import time
from pathlib import Path

import urllib.request

ROOT = Path("~/Stocks").expanduser()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edgar_identity import UA  # SEC contact identity, config-driven
KEY_FORMS = ["10-K", "10-Q", "DEF 14A", "8-K"]
N_PER_FORM = {"10-K": 1, "10-Q": 3, "DEF 14A": 1, "8-K": 5}
# XBRL us-gaap tags worth a time series (first match wins per concept)
FACT_MAP = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "op_income": ["OperatingIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt", "DebtInstrumentCarryingAmount"],
    "equity": ["StockholdersEquity"],
    "shares": ["CommonStockSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
}
ITEM_RE = re.compile(r"(?im)^\s*(item\s+(?:1a?|2|3|5|7a?|8|9a?)\.?[^\n]{0,80})$")


def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read()
    time.sleep(0.15)  # EDGAR politeness
    return data if binary else data.decode("utf-8", errors="replace")


def cik_for(tk):
    m = json.loads(get("https://www.sec.gov/files/company_tickers.json"))
    for v in m.values():
        if v["ticker"].upper() == tk:
            return str(v["cik_str"]).zfill(10), v["title"]
    raise SystemExit(f"{tk}: not in SEC ticker map")


def strip_html(raw):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(p|div|tr|table|h\d|li|br)[^>]*>", "\n", raw)
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = htmllib.unescape(txt)
    txt = re.sub(r"[ \t\xa0]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


def build(tk, skip_local=False):
    tk = tk.upper()
    cik, title = cik_for(tk)
    company_dirs = [d for d in ROOT.glob(f"*-{tk}") if d.is_dir()]
    base = company_dirs[0] if company_dirs else ROOT / f"{re.sub(r'[^A-Za-z0-9]', '', title.title())}-{tk}"
    ev = base / "research" / "_evidence"
    (ev / "filings").mkdir(parents=True, exist_ok=True)

    # --- filings index -> pick the key set ---
    sub = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    rec = sub["filings"]["recent"]
    picked, counts = [], {f: 0 for f in KEY_FORMS}
    for form, date, acc, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"]):
        if form in counts and counts[form] < N_PER_FORM[form] and doc:
            counts[form] += 1
            picked.append({"form": form, "date": date, "acc": acc, "doc": doc})
        if all(counts[f] >= N_PER_FORM[f] for f in counts):
            break

    sections = {}
    for f in picked:
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}/{f['doc']}"
        try:
            txt = strip_html(get(url))
        except Exception as e:
            f["error"] = str(e)[:80]
            continue
        # 3M-char sanity cap only (a stripped 10-K is ~600k). The old 400k cap silently
        # dropped Part II Items 5/9B — caught by the FLYW adversarial review 2026-08-11.
        if len(txt) > 3_000_000:
            txt = txt[:3_000_000]
            f["truncated"] = True
        name = f"{f['date']}_{f['form'].replace(' ', '').replace('/', '')}.txt"
        (ev / "filings" / name).write_text(txt)
        f["file"] = f"filings/{name}"
        f["chars"] = len(txt)
        sections[name] = [{"item": m.group(1).strip()[:70], "offset": m.start()}
                          for m in ITEM_RE.finditer(txt)][:40]

    # --- XBRL facts -> tidy series ---
    facts_out = {}
    try:
        cf = json.loads(get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"))
        gaap = cf.get("facts", {}).get("us-gaap", {})
        for concept, tags in FACT_MAP.items():
            for tag in tags:
                units = gaap.get(tag, {}).get("units", {})
                vals = units.get("USD") or units.get("shares") or []
                rows = [v for v in vals if v.get("form") in ("10-K", "10-Q") and v.get("end")]
                if rows:
                    seen = {}
                    for v in rows:
                        key = (v["end"], v.get("fp", ""))
                        seen[key] = {"end": v["end"], "val": v["val"], "fy": v.get("fy"),
                                     "fp": v.get("fp"), "form": v.get("form")}
                    facts_out[concept] = {"tag": tag, "series": sorted(seen.values(), key=lambda x: x["end"])[-24:]}
                    break
    except Exception as e:
        facts_out["_error"] = str(e)[:120]
    (ev / "facts.json").write_text(json.dumps(facts_out, indent=1))
    (ev / "sections.json").write_text(json.dumps(sections, indent=1))

    # --- code-built INDEX skeleton (local-model notes appended by navindex.py) ---
    idx = [f"# Evidence pack — {title} ({tk}) · built {dt.date.today().isoformat()}",
           "", "_Navigation map for research agents: read from here first; anything can still be",
           "pulled raw from EDGAR — this pack narrows the search, it never limits it._", "",
           "## Financial series (facts.json)",
           ", ".join(k for k in facts_out if not k.startswith("_")) or "(XBRL fetch failed)",
           "", "## Filings in the pack"]
    for f in picked:
        if f.get("file"):
            secs = sections.get(Path(f["file"]).name, [])
            idx.append(f"- **{f['form']} {f['date']}** → `{f['file']}` ({f['chars']:,} chars; "
                       f"{len(secs)} located items)")
    (ev / "INDEX.md").write_text("\n".join(idx) + "\n")
    manifest = {"ticker": tk, "cik": cik, "title": title, "built": dt.datetime.now().isoformat(timespec="seconds"),
                "filings": picked, "facts_concepts": [k for k in facts_out if not k.startswith("_")]}
    (ev / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"evidence pack: {ev} · {len([f for f in picked if f.get('file')])} filings · "
          f"{len(manifest['facts_concepts'])} fact series")

    # codified digits-by-date for the research-book teardown too (David 2026-08-12) — same
    # fincard the agent uses: XBRL + quote, formulas attached, pure code
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "valuation"))
        import fincard
        (ev / "fincard.json").write_text(json.dumps(fincard.build(tk, cik), indent=1))
        print("fincard.json: codified figures + mechanical valuation grid")
    except Exception as e:
        print(f"(fincard skipped: {str(e)[:80]})")

    if not skip_local:
        try:
            import navindex
            navindex.annotate(ev)
        except Exception as e:
            print(f"(local nav notes skipped: {str(e)[:80]})")
    return ev


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: evidence.py TICKER [--skip-local]")
    build(sys.argv[1], skip_local="--skip-local" in sys.argv)
