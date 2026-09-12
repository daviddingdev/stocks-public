#!/usr/bin/env python3
"""
Per-name evidence database + claim auditor — the agent's ANALYST layer.

Zero Claude tokens. Local model (via ~/maintenance/bin/localllm) does the
reading; CODE does the verifying. The division of labor is the point:

  the local model may only ASSERT things it can QUOTE, and code checks every
  quote verbatim against the document text. A quote that doesn't appear in
  the filing is flagged, never trusted. (Born from the 2026-08-12 LBRDP
  incident: a redemption term asserted from model memory sized 28% of the
  book. See journal/decisions.md.)

Layout (gitignored — bulky, regenerable):
  names/<TICKER>/
    manifest.json    what's in the dossier, CIK, built-at
    filings/*.txt    text-stripped primary docs (10-K, 10-Qs, 8-Ks, proxies, 8-A)
    facts.json       XBRL time series straight from SEC companyfacts
    terms.json       security/contract terms extracted by the local model,
                     each with a verbatim quote + verified:true/false

Commands:
  build TICKER [--cik N]  build/refresh the dossier for a name
  audit MEMO.md [TICKER]  extract the memo's load-bearing factual claims
                          (or read its "## Claims" block if present) and
                          verify each against the dossier: SUPPORTED /
                          CONTRADICTED / NOT_FOUND, with a code-verified
                          quote. Writes <memo>.audit.json next to the memo.
                          Exit 1 if any claim is CONTRADICTED or NOT_FOUND.
  status                  list dossiers and their ages

The trade loop (loop.py TRADE_PROMPT) requires: build before entering a new
name, audit before placing any order. reconcile() checks post-hoc that every
placed order's memo has a clean audit — an unaudited or failed memo is
flagged to David like a fabricated order state.
"""
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES = HERE / "names"
JOURNAL = HERE / "journal"
sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
from localllm import ask_json, DEFAULT_MODEL  # noqa: E402
import feeds                    # noqa: E402  (cik_map cache, UA)

UA = feeds.UA
# forms worth having on file per name (count each). 8-A = security terms;
# PREM/DEFM14A = deal terms — exactly the class of document the LBRDP error never read.
# 10-12B(/A) = Form 10 registration for a spin-off; its EX-99.1 is the information statement.
FORM_COUNTS = {"10-K": 1, "10-Q": 2, "8-K": 4, "DEF 14A": 1, "DEFM14A": 1,
               "PREM14A": 1, "8-A12B": 1, "8-A12G": 1, "10-12B": 1, "10-12B/A": 1}
# filings whose primaryDocument is only the cover/body — the load-bearing numbers sit in
# an EX-99.x exhibit (earnings/supplemental-financial exhibits, spin-off info statements)
# that SEC's submissions API never lists (dossier.py-096: MBGL's Ex-99.2 supplemental
# financials and Form 10 info statement were both silently absent from every dossier).
EXHIBIT_FORMS = {"8-K", "10-12B", "10-12B/A"}
EXHIBIT_TYPE_RE = re.compile(r"^EX-99(\.\d+)?$", re.I)

# DEBT-NOTE EXHIBIT INCORPORATION (dossier.py-200): the exhibit INDEX (Item 15) names every
# material contract by exhibit number plus, for anything not filed WITH this report, the
# prior filing it was incorporated from. A credit agreement's covenant schedule lives ONLY
# in that exhibit, never in the note's own prose (MYGN's OrbiMed Credit Agreement, PM
# 2026-09-11: the covenant ladder that moved a modelled breach a quarter was in Ex-10.1 to
# the 8-K filed 2025-07-31; the 10-K/10-Q never quotes it). Fetched the same way EX-99.1
# press releases already land. Scope, deliberately narrow: Ex-10.x whose INDEX DESCRIPTION
# names a debt keyword — a benefits plan or employment agreement under Ex-10 is not fetched
# — and only the two citation shapes actually observed on disk (table cell with an explicit
# filing date; prose "Filed as Exhibit X to Form Y ... filed [on] DATE" or "... for the
# quarter/year ended DATE"). A citation this doesn't parse is silently skipped, not guessed.
EX10_TYPE_RE = re.compile(r"^EX-10(\.\d+)?$", re.I)
DEBT_EXHIBIT_KEYWORDS_RE = re.compile(
    r"(?i)\b(credit agreement|indenture|loan agreement|credit facility|term loan)\b")
EXHIBIT_ANCHOR_RE = re.compile(r"(?m)^\s*(10\.\d{1,3})\s*$")
EXHIBIT_TABLE_CITE_RE = re.compile(
    r"(?i)\b(8-K|10-K|10-Q|10-12B(?:/A)?|S-1|S-4|DEF ?14A)\s*\(Exhibit\s+(10\.\d{1,3}(?:\.\d+)?)\)")
EXHIBIT_PROSE_CITE_RE = re.compile(
    r"(?i)exhibit\s+(10\.\d{1,3}(?:\.\d+)?)[^\n]{0,80}?"
    r"form\s+(8-K|10-K|10-Q|10-12B(?:/A)?|S-1|S-4|DEF ?14A)[^\n]{0,80}?"
    r"filed\s+(?:on\s+)?(\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]+ \d{1,2},\s*\d{4})")
EXHIBIT_PROSE_PERIOD_CITE_RE = re.compile(
    r"(?i)exhibit\s+(10\.\d{1,3}(?:\.\d+)?)[^\n]{0,80}?"
    r"form\s+(8-K|10-K|10-Q|10-12B(?:/A)?|S-1|S-4|DEF ?14A)[^\n]{0,80}?"
    r"(?:quarter|year)\s+ended\s+(\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]+ \d{1,2},\s*\d{4})")
EXHIBIT_CITE_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
EXHIBIT_FILED_HERE_RE = re.compile(r"(?m)^\s*X\s*$")


def _parse_cite_date(s):
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def debt_exhibit_refs(txt):
    """Ex-10.x rows in a 10-K/10-Q's own Exhibit Index whose description names a debt
    instrument. Returns [{"own": "10.24", "ref_form": "8-K"|None, "ref_exhibit": "10.1"|None,
    "ref_date": "2025-07-31"|None, "by": "filing_date"|"period_date"|"filed_here"}, ...].
    ref_form/ref_exhibit/ref_date are None when the exhibit is filed WITH this report
    (marked "X" in the index rather than incorporated by reference)."""
    anchors = list(EXHIBIT_ANCHOR_RE.finditer(txt))
    out = []
    for i, m in enumerate(anchors):
        start = m.end()
        end = min(start + 500, anchors[i + 1].start() if i + 1 < len(anchors) else start + 500)
        block = txt[start:end]
        table_cite = EXHIBIT_TABLE_CITE_RE.search(block)
        prose_cite = EXHIBIT_PROSE_CITE_RE.search(block)
        prose_period = EXHIBIT_PROSE_PERIOD_CITE_RE.search(block)
        filed_here = EXHIBIT_FILED_HERE_RE.search(block)
        candidates = [c for c in (table_cite, prose_cite, prose_period, filed_here) if c]
        if not candidates:
            continue
        first = min(candidates, key=lambda c: c.start())
        desc = block[:first.start()]
        if not DEBT_EXHIBIT_KEYWORDS_RE.search(desc):
            continue
        own = m.group(1)
        if first is table_cite:
            date_m = EXHIBIT_CITE_DATE_RE.search(block, table_cite.end())
            out.append({"own": own, "ref_form": table_cite.group(1).upper(),
                        "ref_exhibit": table_cite.group(2),
                        "ref_date": _parse_cite_date(date_m.group(0)) if date_m else None,
                        "by": "filing_date"})
        elif first is prose_cite:
            out.append({"own": own, "ref_form": prose_cite.group(2).upper(),
                        "ref_exhibit": prose_cite.group(1),
                        "ref_date": _parse_cite_date(prose_cite.group(3)), "by": "filing_date"})
        elif first is prose_period:
            out.append({"own": own, "ref_form": prose_period.group(2).upper(),
                        "ref_exhibit": prose_period.group(1),
                        "ref_date": _parse_cite_date(prose_period.group(3)), "by": "period_date"})
        else:  # filed_here
            out.append({"own": own, "ref_form": None, "ref_exhibit": None, "ref_date": None,
                        "by": "filed_here"})
    return out


def resolve_exhibit_filing(rec, ref):
    """A debt_exhibit_refs() row -> (form, filingDate, accessionNumber) of the filing that
    actually carries the exhibit, searched in the submissions API's `recent` window. None if
    unresolved (older than the recent window, or the citation didn't carry a usable date)."""
    if ref["by"] == "filed_here" or not ref.get("ref_date"):
        return None
    key = "filingDate" if ref["by"] == "filing_date" else "reportDate"
    matches = [(f, d, a) for f, d, a in zip(rec["form"], rec[key], rec["accessionNumber"])
               if f == ref["ref_form"] and d == ref["ref_date"]]
    if len(matches) != 1:
        return None
    f, _, a = matches[0]
    fd = rec["filingDate"][rec["accessionNumber"].index(a)]
    return f, fd, a

# WALL-CLOCK BUDGET (dossier.py-153, coo 2026-09-06 / numbers 2026-09-08): vp.py wraps
# the dossier stage in the SAME 900s hard subprocess kill that motivated
# refresh_cards.py-137's BUDGET_S — the exhibit-fetch loop below is the unbounded part
# (list_exhibits() is ONE more network round trip per filing, then ANOTHER fetch per
# EX-99.x found), and on a night SEC is slow (the same "~30KB/s, 87-242s per card"
# degradation refresh_cards.py-137 measured 2026-09-04/05) the sum blows past 900s with
# NOTHING written — no terms.json, no stub, silent, on ARI specifically twice
# (2026-09-04/05), the book's largest position. Budgeted to 650s, leaving ~250s of
# margin under the external kill for the fixed-cost tail after this loop (companyfacts
# fetch, fincard.build()'s OWN companyfacts fetch, extract_terms) — narrower than
# refresh_cards.py's 300s margin because that margin only had to cover one ticker's
# worst observed single call (242s); this margin has to cover THREE more network calls
# plus local text processing. Once tripped, remaining PRIMARY filings and remaining
# EXHIBIT fetches are both skipped (primary filings already fetched stay; nothing
# already on disk is discarded) and every skip is recorded on its own row so a partial
# dossier says exactly what it does and doesn't have, same as refresh_cards.py's stub.
BUDGET_S = 650


def list_exhibits(cik, acc, type_re=EXHIBIT_TYPE_RE):
    """Exhibits matching type_re in a filing's index page: [(type, document_filename), ...].
    Default EXHIBIT_TYPE_RE (EX-99.x); pass EX10_TYPE_RE for a debt-exhibit lookup."""
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm"
    try:
        idx = get(url)
    except Exception:
        return []
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", idx):
        cells = [htmllib.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
        if len(cells) >= 4 and type_re.match(cells[3]):
            out.append((cells[3].upper(), cells[2]))
    return out
FACT_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "net_income": ["NetIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "equity": ["StockholdersEquity"],
    "shares": ["CommonStockSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
}
# keyword families that locate term-bearing passages (code finds, model reads)
TERM_KEYWORDS = ["redemption", "redeem", "redeemable", "conversion", "convert",
                 "exchange ratio", "liquidation preference", "change of control",
                 "dividend rate", "cumulative", "tender offer", "dissolution",
                 "distribution", "maturity", "call date", "par value"]
# DEFM14A/PREM14A carry the DEAL the position IS, not a security's coupon terms — a proxy
# can be 51/51 quote-verified on redemption/liquidation-preference language from an OLD
# preferred while never reading the vote it is actually about (dossier.py-126, ARI: 18 rows
# on a 2016 preferred redeemed 2026-07-15, zero on the $7.75-8.50 distribution range or the
# 2026-09-29 Special Meeting). These keywords only fire on DEFM14A/PREM14A docs, additive to
# TERM_KEYWORDS above.
DEAL_KEYWORDS = ["estimated total stockholder distributions", "distribution range",
                 "special meeting", "record date", "majority of all the votes",
                 "broker non-votes", "abstentions", "plan of dissolution",
                 "plan of liquidation", "liquidating trust", "non-transferable",
                 "asset sale", "dissolution proposal"]
DEAL_FORMS = ("DEFM14A", "PREM14A")
STOP = set("the a an and or of to in for on by with as at from that this is are was were be been "
           "has have had its it their which will shall may any all such per share shares company".split())


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read()
    time.sleep(0.15)  # EDGAR politeness
    return data.decode("utf-8", errors="replace")


def strip_html(raw):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(p|div|tr|table|h\d|li|br)[^>]*>", "\n", raw)
    txt = htmllib.unescape(re.sub(r"<[^>]+>", " ", raw))
    txt = re.sub(r"[ \t\xa0]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


def norm(s):
    """Whitespace/case-insensitive form used for verbatim-quote checking."""
    return re.sub(r"[^a-z0-9$%.]+", " ", s.lower()).strip()


def resolve_cik(tk, override=None):
    """Ticker -> CIK. Handles share classes (LBRDP -> LBRDA's CIK) with a fallback."""
    if override:
        return str(override).zfill(10), tk
    m = feeds.cik_map()
    for cand in (tk, tk[:-1] + "A", tk[:-1] + "K", tk[:-1] + "B", tk[:-1]):
        if len(cand) >= 2 and m.get(cand):
            return m[cand], cand
    raise SystemExit(f"{tk}: no CIK found (share class? pass --cik N)")


# ---------------- build ----------------
def build(tk, cik_override=None):
    t0 = time.time()
    tk = tk.upper()
    cik, via = resolve_cik(tk, cik_override)
    d = NAMES / tk
    (d / "filings").mkdir(parents=True, exist_ok=True)

    sub = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    title = sub.get("name", tk)
    rec = sub["filings"]["recent"]
    picked, counts = [], {f: 0 for f in FORM_COUNTS}
    for form, date, acc, doc in zip(rec["form"], rec["filingDate"],
                                    rec["accessionNumber"], rec["primaryDocument"]):
        if form in counts and counts[form] < FORM_COUNTS[form] and doc:
            counts[form] += 1
            picked.append({"form": form, "date": date, "acc": acc, "doc": doc})
    extra = []
    budget_tripped = False
    for i, f in enumerate(picked):
        if time.time() - t0 > BUDGET_S:
            budget_tripped = True
            for rest in picked[i:]:
                rest["skipped"] = f"BUDGET ({BUDGET_S}s) tripped before this filing was fetched (dossier.py-153)"
            break
        form_clean = f["form"].replace(" ", "").replace("/", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}/{f['doc']}"
        try:
            txt = strip_html(get(url))
        except Exception as e:
            f["error"] = str(e)[:80]
            continue
        if len(txt) > 3_000_000:
            txt, f["truncated"] = txt[:3_000_000], True
        name = f"{f['date']}_{form_clean}.txt"
        (d / "filings" / name).write_text(txt)
        f["file"], f["chars"] = f"filings/{name}", len(txt)

        if f["form"] not in EXHIBIT_FORMS and f["form"] not in ("10-K", "10-Q"):
            continue
        if budget_tripped or time.time() - t0 > BUDGET_S:
            budget_tripped = True
            f["exhibits_skipped"] = f"BUDGET ({BUDGET_S}s) tripped before exhibits were enumerated (dossier.py-153)"
            continue

        if f["form"] in EXHIBIT_FORMS:
            for ex_type, ex_doc in list_exhibits(cik, f["acc"]):
                if ex_doc == f["doc"]:
                    continue  # already fetched as the primary document
                ex_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}/{ex_doc}"
                ex = {"form": f["form"], "date": f["date"], "acc": f["acc"], "doc": ex_doc, "exhibit": ex_type}
                try:
                    ex_txt = strip_html(get(ex_url))
                except Exception as e:
                    ex["error"] = str(e)[:80]
                    extra.append(ex)
                    continue
                if len(ex_txt) > 3_000_000:
                    ex_txt, ex["truncated"] = ex_txt[:3_000_000], True
                ex_name = f"{f['date']}_{form_clean}_{ex_type.replace(' ', '')}.txt"
                (d / "filings" / ex_name).write_text(ex_txt)
                ex["file"], ex["chars"] = f"filings/{ex_name}", len(ex_txt)
                extra.append(ex)
                if time.time() - t0 > BUDGET_S:
                    budget_tripped = True
                    break
            continue

        # dossier.py-200: this 10-K/10-Q's own Exhibit Index may name a debt-note exhibit
        # (Ex-10.x) that never appears in EXHIBIT_FORMS because it rides on a 10-K/10-Q, not
        # an 8-K. Resolve each ref to the filing that actually carries it — the current
        # accession if filed with this report, else whatever (form, date) the index cites —
        # and fetch it the same way an EX-99.x exhibit lands above.
        for ref in debt_exhibit_refs(txt):
            if ref["by"] == "filed_here":
                ex_form, ex_date, ex_acc, ex_num = f["form"], f["date"], f["acc"], ref["own"]
            else:
                resolved = resolve_exhibit_filing(rec, ref)
                if not resolved:
                    extra.append({"form": f["form"], "date": f["date"], "acc": f["acc"],
                                   "exhibit": f"EX-{ref['ref_exhibit']}", "own": ref["own"],
                                   "error": f"debt exhibit ref unresolved (wanted {ref['ref_form']} "
                                            f"@ {ref['ref_date']}, {ref['by']}) — not in the "
                                            "submissions API's recent window, or ambiguous"})
                    continue
                ex_form, ex_date, ex_acc = resolved
                ex_num = ref["ref_exhibit"]
            ex_form_clean = ex_form.replace(" ", "").replace("/", "")
            ex_name = f"{ex_date}_{ex_form_clean}_EX-{ex_num}.txt"
            if (d / "filings" / ex_name).exists():
                continue  # already on disk from a prior build
            found = next((doc for typ, doc in list_exhibits(cik, ex_acc, EX10_TYPE_RE)
                          if typ.upper()[3:] == ex_num), None)
            ex = {"form": ex_form, "date": ex_date, "acc": ex_acc, "exhibit": f"EX-{ex_num}",
                  "cited_from": f["file"]}
            if not found:
                ex["error"] = f"EX-{ex_num} not found in {ex_acc}'s own index"
                extra.append(ex)
                continue
            ex_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{ex_acc.replace('-', '')}/{found}"
            try:
                ex_txt = strip_html(get(ex_url))
            except Exception as e:
                ex["error"] = str(e)[:80]
                extra.append(ex)
                continue
            if len(ex_txt) > 3_000_000:
                ex_txt, ex["truncated"] = ex_txt[:3_000_000], True
            (d / "filings" / ex_name).write_text(ex_txt)
            ex["doc"], ex["file"], ex["chars"] = found, f"filings/{ex_name}", len(ex_txt)
            extra.append(ex)
            if time.time() - t0 > BUDGET_S:
                budget_tripped = True
                break
    picked.extend(extra)

    facts = {}
    try:
        gaap = json.loads(get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")) \
            .get("facts", {}).get("us-gaap", {})
        for concept, tags in FACT_TAGS.items():
            for tag in tags:
                units = gaap.get(tag, {}).get("units", {})
                vals = [v for v in (units.get("USD") or units.get("shares") or [])
                        if v.get("form") in ("10-K", "10-Q") and v.get("end")]
                if vals:
                    seen = {(v["end"], v.get("fp", "")): {"end": v["end"], "val": v["val"], "fp": v.get("fp")}
                            for v in vals}
                    facts[concept] = {"tag": tag,
                                      "series": sorted(seen.values(), key=lambda x: x["end"])[-16:]}
                    break
    except Exception as e:
        facts["_error"] = str(e)[:100]
    (d / "facts.json").write_text(json.dumps(facts, indent=1))

    # codified digits-by-date (David 2026-08-12): every number the PM uses comes from
    # code with tag+period+formula attached — never from a model's working memory
    try:
        sys.path.insert(0, str(HERE.parent / "valuation"))
        import fincard
        card = fincard.build(tk, cik)
        (d / "fincard.json").write_text(json.dumps(card, indent=1))
        fincheck(d, card)
    except Exception as e:
        print(f"(fincard skipped: {str(e)[:80]})")

    terms = extract_terms(d, title, deadline=t0 + BUDGET_S)
    budget_tripped = budget_tripped or bool(terms.get("budget_skipped"))
    if terms.get("budget_skipped"):
        # MERGE, don't overwrite (dossier.py-153): extract_terms() only re-examines
        # docs it had budget for — a budget-tripped run's "terms" list is a fresh look
        # at 1-2 docs, not a full pass. Live-tested on ARI: two consecutive
        # budget-tripped runs landed on DIFFERENT single docs (one on the 10-Q, 23
        # terms; the next on an 8-K exhibit that failed to parse, 0 terms) — writing
        # THAT list straight to terms.json would have ERASED the prior run's 23 terms
        # the moment a night got unlucky, turning a coverage gap into active data
        # loss. Keep the OLD entry for any doc this run never got to (budget_skipped,
        # and still present in filings/); this run's terms — including an empty
        # result — replace the old entry for whichever doc(s) it DID re-examine,
        # since a fresh look supersedes a stale one either way.
        try:
            old = json.loads((d / "terms.json").read_text())
        except Exception:
            old = {}
        skipped, on_disk = set(terms["budget_skipped"]), {p.name for p in (d / "filings").glob("*.txt")}
        carried = [t for t in old.get("terms", []) if t.get("doc") in skipped and t.get("doc") in on_disk]
        if carried:
            terms["terms"] = carried + terms["terms"]
            terms["carried_from_prior_run"] = sorted({t["doc"] for t in carried})
    (d / "terms.json").write_text(json.dumps(terms, indent=1))
    (d / "manifest.json").write_text(json.dumps(
        {"ticker": tk, "cik": cik, "resolved_via": via, "title": title,
         "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "filings": picked, "n_terms": len(terms.get("terms", [])),
         "n_verified": sum(1 for t in terms.get("terms", []) if t.get("verified")),
         # BUDGET (dossier.py-153): visible on the manifest, not just buried per-filing
         # skip notes — a partial dossier must say so where the VP brief/contract
         # checks already look, not require grepping 14 filing rows to notice.
         "budget_tripped": budget_tripped, "build_seconds": round(time.time() - t0)},
        indent=1))
    print(f"dossier {tk}: {len([f for f in picked if f.get('file')])} filings · "
          f"{len(facts)} fact series · {len(terms.get('terms', []))} terms "
          f"({sum(1 for t in terms.get('terms', []) if t.get('verified'))} quote-verified)"
          + (f" · BUDGET TRIPPED at {BUDGET_S}s (dossier.py-153) — partial" if budget_tripped else ""))
    return d


def fincheck(d, card):
    """Local model validates the fincard's XBRL tag-picking against the FILING TEXT:
    reads statement passages from the latest 10-Q/10-K, extracts the reported cash /
    CFO / capex / long-term debt, and code compares to the card (scale-aware —
    filings print in thousands/millions). Catches wrong-tag and wrong-scale picks.
    Writes fincard_check.json; mismatches are flags for the PM, not silent trust."""
    docs = sorted(d.glob("filings/*10-[QK]*.txt"), reverse=True)
    if not docs:
        return
    txt = docs[0].read_text(errors="replace")
    # debt captions are issuer-specific: "long-term debt"/"total debt" appear nowhere on a
    # mortgage REIT's or a lessor's balance sheet, so the model was handed windows with no
    # debt line and invented one (ARI: "1,221,185", absent from the 10-Q — 2026-08-13)
    wins = keyword_windows(txt, ["cash and cash equivalents", "net cash provided by",
                                 "operating activities", "purchases of property",
                                 "long-term debt", "total debt", "secured debt",
                                 "notes payable", "senior notes", "term loan",
                                 "debt related to", "total liabilities"], width=1500, cap=5)
    if not wins:
        return
    v = ask_json(
        f"From these excerpts of {card.get('entity')}'s filing ({docs[0].name}), extract the "
        "REPORTED values. RULES: copy digits EXACTLY as printed and never add, subtract, "
        "combine or round them; a value you cannot find printed in the excerpts is NOT_SHOWN. "
        "Balance-sheet tables print two date columns — the FIRST number after a caption is the "
        "MOST RECENT date; never take the second (prior-year) column. Note the table's stated "
        "scale ('in thousands'/'in millions') if visible. Extract: cash & cash equivalents "
        "(balance sheet); net cash provided by operating activities; purchases of property & "
        "equipment (capex); and debt_lt = the balance sheet's NONCURRENT (long-term) borrowings "
        "line, whatever it is called at this issuer — 'Long-term debt', 'Notes payable', "
        "'Secured debt arrangements, net', 'Senior secured notes, net', 'Term loan', 'Debt "
        "related to real estate owned' — copying the SINGLE largest current-column debt line. "
        "If a debt note shows BOTH a gross/total debt line AND a 'net of current portion' (or "
        "'noncurrent') line for the same column, copy the NET-OF-CURRENT-PORTION (noncurrent) "
        "figure, never the gross total — debt_lt excludes the current portion by definition. "
        "If every debt line shows a dash or the balance sheet has none, debt_lt is NOT_SHOWN. "
        "A Chapter 11 filer may print a SUPPLEMENTAL 'Debtors-only' or 'Non-Debtor Affiliates' "
        "cash-flow schedule (flagged by its own footnote, e.g. a superscript '(1)' next to the "
        "caption, saying it EXCLUDES certain affiliates' cash flows) alongside the primary "
        "Condensed Consolidated Statements of Cash Flows — that supplemental schedule's cfo/"
        "capex figures are NOT the reported consolidated values; if the consolidated statement "
        "isn't ALSO shown in these excerpts, answer NOT_SHOWN for that line rather than copying "
        "the supplemental schedule's number (QVCG 2026-09-12: a Debtors-only table's $(2)M was "
        "mistaken for consolidated CFO of $56M, a false fincheck MISMATCH against the correct "
        "TTM-derived card figure). "
        "JSON {\"cash\":\"<digits or NOT_SHOWN>\",\"cfo\":\"...\",\"capex\":"
        "\"...\",\"debt_lt\":\"...\",\"scale\":\"thousands|millions|units|unknown\"}\n\n"
        + "\n\n[---]\n\n".join(wins), num_predict=500, think=True, job="dossier fincheck")
    if not isinstance(v, dict):
        return
    out = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "doc": docs[0].name, "model": "qwen (local) extracts, code compares", "checks": {}}
    F, SER = card.get("figures", {}), card.get("series", {})
    txt_digits = re.sub(r"[,\s]", "", txt)  # verbatim-quote doctrine (see module docstring):
    # the model may only ASSERT what it can QUOTE — a doc_raw that isn't literally in the
    # filing is an extraction hallucination, not a real mismatch (ARI debt_lt: model produced
    # 1,221,185, absent from the 10-Q text entirely — caught 2026-08-13)
    for key in ("cash", "cfo", "capex", "debt_lt"):
        raw = re.sub(r"[^\d.]", "", str(v.get(key, "")))
        cardv = (F.get(key) or {}).get("value")
        if not raw or cardv is None:
            out["checks"][key] = {"status": "not_compared", "doc_raw": v.get(key), "card": cardv}
            continue
        if (F.get(key) or {}).get("source") == "MANUAL — PM-verified":
            # a MANUAL figure (fincard.py's MANUAL/manual_overrides.json) may be the SUM of
            # two additive instruments quoted from two different lines (CVU: Line of credit
            # 9,173,672 + Long-term debt net of current portion 9,578,051 = 18,751,723) —
            # comparing it against whichever SINGLE line this extraction happened to read
            # will always MISMATCH even though the card is right. The MANUAL entry already
            # carries its own verbatim quote + doc; that IS the verification, so this check
            # defers to it instead of re-flagging a sum against one of its addends.
            out["checks"][key] = {"status": "manual", "doc_raw": v.get(key), "card": cardv,
                                  "note": "card figure is MANUAL (PM-verified, quoted) — not "
                                          "re-validated against this single-line extraction"}
            continue
        if raw not in txt_digits:
            out["checks"][key] = {"status": "extraction_unverified", "doc_raw": v.get(key),
                                  "card": cardv,
                                  "note": "extracted digits NOT FOUND verbatim in the filing — "
                                          "hallucinated extraction; UN-QUOTABLE (2026-08-13: a "
                                          "phantom ARI debt figure laundered into a session "
                                          "mandate as fact via a quiet version of this status)"}
            continue
        docn = float(raw)
        # 10-Qs print flow figures as quarter or fiscal-YTD, while the card headline is
        # TTM — compare against every period the card's own series can construct
        cands = {"headline": abs(cardv)}
        qs = [q["value"] for q in (SER.get(key) or {}).get("quarters", [])[:4]]
        for n in range(1, len(qs) + 1):
            cands[f"sum_last_{n}q"] = abs(sum(qs[:n]))
        match = next((f"{lbl} x{scale:g}" for lbl, cv in cands.items()
                      for scale in (1, 1e3, 1e6)
                      if cv and abs(docn * scale - cv) / max(cv, 1) < 0.02), None)
        out["checks"][key] = {"status": "ok" if match else "MISMATCH",
                              "doc_raw": v.get(key), "card": cardv,
                              **({"matched": match} if match else {})}
    n_bad = sum(1 for c in out["checks"].values() if c["status"] == "MISMATCH")
    (d / "fincard_check.json").write_text(json.dumps(out, indent=1))
    print(f"fincheck: {len(out['checks'])} figures vs {docs[0].name} — "
          f"{n_bad} MISMATCH" if n_bad else
          f"fincheck: {len(out['checks'])} figures vs {docs[0].name} — all consistent")


def keyword_windows(txt, keywords, width=1200, cap=4, total_cap=14000, per_group=3):
    """Code locates candidate passages; the model only reads these.

    Windows are built PER KEYWORD, rarest keyword first (a rarer keyword is a more
    specific — higher-signal — anchor), and EVERY keyword group is capped at
    `per_group` windows so no single group can exhaust the whole budget before the
    others get a turn. Both limits matter: without the per-group cap, a keyword with
    a middling hit count (e.g. "record date", 53 hits -> 30 windows on its own) can
    still fill the entire cap before a later, equally relevant group (e.g. "special
    meeting", 283 hits, but whose FIRST hit in an ARI proxy is literally "NOTICE OF
    SPECIAL MEETING ... TO BE HELD ON SEPTEMBER 29, 2026") ever gets a window
    (dossier.py-126). Without the merge-span cap, a single dense group can chain
    every early hit into one giant window that alone exceeds the budget."""
    low = txt.lower()
    max_span = width * 3

    def merge(spots):
        windows, last_end = [], -1
        for s in sorted(spots):
            a, b = max(0, s - width // 2), min(len(txt), s + width)
            # snap to line boundaries: a raw character offset can slice a caption in
            # half (VSNT: window started mid-word inside "Long-term debt 2,841", the
            # model never saw the caption and picked a different, fully-visible "Total
            # long-term debt" figure elsewhere instead — quality.py
            # fincheck-mismatch:VSNT:debt_lt, 2026-09-05). Extending to the enclosing
            # line never drops information, only adds a little more of it.
            nl = txt.rfind("\n", 0, a)
            a = nl + 1 if nl != -1 else 0
            nl = txt.find("\n", b)
            b = nl if nl != -1 else len(txt)
            if a < last_end and b - windows[-1][0] <= max_span:
                windows[-1] = (windows[-1][0], b)
            else:
                windows.append((a, b))
            last_end = b
        return windows

    groups = []
    for kw in keywords:
        spots = [m.start() for m in re.finditer(re.escape(kw), low)]
        if spots:
            groups.append(spots)
    groups.sort(key=len)  # rarest keyword's windows first

    out, used, seen = [], 0, []
    for spots in groups:
        taken = 0
        for a, b in merge(spots):
            if taken >= per_group or used >= total_cap or len(out) >= cap * 2:
                break
            if any(a < e and b > s for s, e in seen):   # skip near-duplicate coverage
                continue
            out.append(txt[a:b])
            used += b - a
            seen.append((a, b))
            taken += 1
        if used >= total_cap or len(out) >= cap * 2:
            break
    return out


def extract_terms(d, title, deadline=None):
    """Local model extracts security/contract terms from keyword-located passages.
    Every extraction must carry a verbatim quote; code verifies the quote exists.

    BUDGET (dossier.py-153): this is ONE ask_json call per filing on disk, each
    think=True (extended reasoning — slower per call by design) and up to 36,000
    chars of excerpt for a deal doc. Found live testing this fix (2026-09-08): this
    loop, not the exhibit-fetch loop above, is ARI's real bottleneck — a single
    'dossier terms' call held the GPU slot 90+ seconds with another Stocks job
    (the Bench) already queued behind it, and ARI's filing set (14-18 filings, REIT
    legal documents) means 14-18 such calls in sequence. `deadline` (an absolute
    time.time(), the SAME budget build() already spends on fetching) is checked
    before each call so a tight run stops calling the LLM rather than being killed
    mid-call by vp.py's external 900s cap — partial terms, not zero."""
    out = {"extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "model": f"{DEFAULT_MODEL} (local)", "terms": [], "budget_skipped": []}
    # NEWEST FIRST (dossier.py-153): filenames are "YYYY-MM-DD_FORM[...].txt", so a
    # plain sorted() processes the OLDEST filing first — exactly backwards under a
    # tight budget. Live-tested on ARI: budget allowed exactly 1 of 14 filings before
    # tripping, and plain sort order would have spent that one call on a 2026-03-23
    # DEFM14A while skipping the 2026-08-10 10-Q — the filing that carries the
    # subsequent-events redemption/dividend terms this dossier most needs current
    # (fincard.py-162, same night). A budget-constrained run should capture what's
    # NEW, not what's alphabetically/chronologically first.
    for doc in sorted((d / "filings").glob("*.txt"), reverse=True):
        if deadline is not None and time.time() > deadline:
            out["budget_skipped"].append(doc.name)
            continue
        txt = doc.read_text(errors="replace")
        is_deal_doc = any(f in doc.name for f in DEAL_FORMS)
        if is_deal_doc:
            # DEAL_KEYWORDS windowed in their OWN budgeted pass, narrower width, ahead of
            # TERM_KEYWORDS: in a Plan-of-Dissolution proxy, "special meeting"/"liquidating
            # trust" run 280+ times each and would otherwise outrun rarer TERM_KEYWORDS hits
            # for the same budget before a single deal-term window is ever built.
            wins = (keyword_windows(txt, DEAL_KEYWORDS, width=600, cap=15, total_cap=30000)
                    + keyword_windows(txt, TERM_KEYWORDS, width=900, cap=4, total_cap=6000))
        else:
            wins = keyword_windows(txt, TERM_KEYWORDS)
        if not wins:
            continue
        excerpt = "\n\n[---]\n\n".join(wins)
        deal_ask = (
            " Also extract PLAN/DEAL terms if present: the Estimated Total Stockholder "
            "Distributions Range or any other liquidation/distribution dollar range "
            "(type \"distribution_range\"), the Special Meeting date (type \"meeting_date\"), "
            "the Record Date (type \"record_date\"), the vote/approval threshold required and "
            "how broker non-votes/abstentions are treated (type \"vote_threshold\"), and "
            "Liquidating Trust interest transferability (type \"trust_transferability\")."
        ) if is_deal_doc else ""
        v = ask_json(
            f"These are excerpts from an SEC filing ({doc.name}) for {title}. Extract every "
            "explicit SECURITY or DEAL TERM present: redemption (optional/mandatory, dates, "
            "prices), conversion/exchange ratios, dividend rate & cumulative status, liquidation "
            "preference, change-of-control provisions, tender/dissolution/distribution terms, "
            "maturity/call dates." + deal_ask +
            " Return JSON {\"terms\":[{\"type\":\"...\",\"detail\":\"<one "
            "precise clause with numbers/dates>\",\"quote\":\"<supporting sentence copied "
            "CHARACTER-FOR-CHARACTER from the excerpt, max 40 words>\"}]}. Only terms explicitly "
            "in the text — omit anything you cannot quote. Empty list if none.\n\n" + excerpt,
            num_predict=3000 if is_deal_doc else 2200, think=True, job="dossier terms")
        ntxt = norm(txt)
        for t in (v.get("terms") or []) if isinstance(v, dict) else []:
            q = str(t.get("quote", ""))
            t["doc"] = doc.name
            t["verified"] = bool(q) and norm(q) in ntxt
            out["terms"].append(t)
    return out


# ---------------- audit ----------------
def parse_claims(memo_txt):
    """Prefer an explicit '## Claims' block; else have the local model pull the
    load-bearing verifiable claims out of the prose."""
    m = re.search(r"(?ims)^##\s*Claims\s*$(.+?)(?=^##|\Z)", memo_txt)
    if m:
        rows = []
        for line in m.group(1).splitlines():
            lm = re.match(r"\s*-\s*(?:\[(\w+)\]\s*)?(.+)", line)
            if not lm or len(lm.group(2).strip()) < 10:
                continue
            body = lm.group(2).strip()
            # optional agent-supplied citation, verified deterministically by code:
            #   - [type] claim | quote: "<verbatim>" | doc: <dossier file or EDGAR url>
            row = {"type": (lm.group(1) or "fact").lower()}
            qm = re.search(r"\|\s*quote:\s*\"(.+?)\"", body)
            dm = re.search(r"\|\s*doc:\s*(\S+)", body)
            if qm:
                row["cite_quote"] = qm.group(1)
            if dm:
                row["cite_doc"] = dm.group(1)
            row["claim"] = re.split(r"\s*\|\s*(?:quote|doc):", body)[0].strip()
            rows.append(row)
        if rows:
            return rows, "claims-block"
    v = ask_json(
        "From this pre-trade memo, extract the LOAD-BEARING VERIFIABLE factual claims — "
        "statements about contractual/security terms (redemption, conversion, ratios, "
        "preferences), document-sourced dollar amounts, and dated events (votes, distributions, "
        "deadlines). ONLY claims about the COMPANY/SECURITY that an SEC filing could confirm or "
        "refute. EXCLUDE: the trade's own size/price/share-count/book percentages, current market "
        "quotes, opinions, predictions, and valuation judgments. Max 8, most load-bearing first. "
        "JSON {\"claims\":[{\"type\":\"contractual|numeric|"
        "date\",\"claim\":\"<one sentence>\"}]}\n\n" + memo_txt[:12000], num_predict=800)
    return ([c for c in (v.get("claims") or []) if c.get("claim")] if isinstance(v, dict) else []), "extracted"


def score_paragraphs(txt, claim, n=3, width=900):
    """Cheap retrieval: rank paragraphs by rare-token overlap with the claim.

    Returns (score, chunk) pairs, NOT bare chunks — audit() has to rank candidates
    ACROSS documents before it truncates, and it cannot do that without the score
    (hunt-2026-08-29d)."""
    toks = [w for w in re.findall(r"[a-z0-9$%.]{3,}", claim.lower()) if w not in STOP]
    if not toks:
        return []
    paras, best = txt.split("\n\n"), []
    for i, p in enumerate(paras):
        pl = p.lower()
        s = sum(pl.count(t) for t in set(toks))
        if s:
            best.append((s, i))
    best.sort(reverse=True)
    out = []
    for sc, i in best[:n]:
        chunk = "\n\n".join(paras[max(0, i - 1):i + 2])[:width * 3]
        out.append((sc, chunk))
    return out


def audit(memo_path, tk=None):
    memo = Path(memo_path)
    if not memo.exists():
        memo = JOURNAL / memo_path
    txt = memo.read_text()
    tk = (tk or re.search(r"\d{4}-\d{2}-\d{2}_([A-Z]+)_", memo.name).group(1)).upper()
    d = NAMES / tk
    if not (d / "manifest.json").exists():
        print(f"no dossier for {tk} — building first")
        build(tk)
    docs = {p.name: p.read_text(errors="replace") for p in sorted((d / "filings").glob("*.txt"))}
    terms = json.loads((d / "terms.json").read_text()) if (d / "terms.json").exists() else {}
    claims, how = parse_claims(txt)
    results = []
    for c in claims:
        # 1) deterministic path: the memo cites its own quote + doc — code verifies
        #    verbatim, no model judgment involved. This is the escape valve for
        #    auditor blind spots (doc recency, retrieval misses): read the filing,
        #    quote it in the memo, and the gate passes on evidence alone.
        if c.get("cite_quote"):
            src, ok = c.get("cite_doc", ""), False
            if src in docs:
                ok = norm(c["cite_quote"]) in norm(docs[src])
            elif src.startswith("http") and "sec.gov" in src:
                try:
                    ok = norm(c["cite_quote"]) in norm(strip_html(get(src)))
                except Exception:
                    ok = False
            results.append({**c, "verdict": "SUPPORTED" if ok else "NOT_FOUND",
                            "doc": src, "quote": c["cite_quote"], "quote_verified": ok,
                            "why": "agent citation verified by code" if ok
                                   else "agent citation FAILED verbatim check"})
            continue
        # 2) model path: retrieval across all docs, best-first.
        # hunt-2026-08-29d, fixed in 186abc0: "best-first" was a lie. The per-document top-3
        # were appended in DOCUMENT order and then cut at [:6], and docs is built from
        # sorted(glob) = OLDEST FILENAME FIRST — so the auditor only ever saw the two
        # oldest documents on file and never the ones the claim was written from. Live
        # proof: every one of the 8 claims in 2026-07-29_LBRDP_buy (3 CONTRADICTED, 5
        # NOT_FOUND) was judged against a 2020 8-A12B and a 2024 PREM14A while the
        # 2025-01-22 DEFM14A carrying the merger terms, the 2026-02-05 10-K and the
        # 2026-07-29 10-Q were all discarded unread. Same on ARI (12 docs, the newest ten
        # never reached the model) and on yesterday's 2026-08-28_TLS_sell. Rank the pooled
        # candidates by their own retrieval score, newest document breaking ties, THEN cut.
        scored = []
        for name, doc_txt in docs.items():
            for sc, chunk in score_paragraphs(doc_txt, c["claim"]):
                scored.append((sc, name, chunk))
        # score desc; ties to the NEWEST document (filenames are date-prefixed)
        scored.sort(key=lambda r: r[1], reverse=True)
        scored.sort(key=lambda r: r[0], reverse=True)
        cands = [(name, chunk) for _, name, chunk in scored][:6]
        verdict = {"verdict": "NOT_FOUND", "quote": "", "doc": "", "why": "no relevant passage located"}
        if cands:
            excerpt = "\n\n".join(f"[{n}]\n{ch}" for n, ch in cands)[:16000]
            v = ask_json(
                f"CLAIM to verify: \"{c['claim']}\"\n\nEXCERPTS from {tk}'s SEC filings (doc name "
                "in brackets):\n\n" + excerpt + "\n\nDo the excerpts SUPPORT the claim, CONTRADICT "
                "it, or not address it? Judge ONLY from the text. If the claim concerns a THIRD "
                "PARTY'S filings, a news event, market prices, or anything this issuer's own SEC "
                "filings would never contain, the verdict is OUT_OF_SCOPE (needs a different "
                "source), not NOT_FOUND. Return JSON {\"verdict\":"
                "\"SUPPORTED|CONTRADICTED|NOT_FOUND|OUT_OF_SCOPE\",\"doc\":\"<doc name>\",\"quote\":\"<the "
                "decisive sentence copied CHARACTER-FOR-CHARACTER>\",\"why\":\"<max 15 words>\"}. "
                "If the documents state different terms than the claim asserts, that is "
                "CONTRADICTED, not NOT_FOUND. Reason first: find every excerpt that bears on the "
                "claim, decide what each one says, then choose the decisive sentence.",
                num_predict=600, think=True, job="dossier adjudicate")
            if isinstance(v, dict) and v.get("verdict") in ("SUPPORTED", "CONTRADICTED", "NOT_FOUND", "OUT_OF_SCOPE"):
                verdict = v
        q = str(verdict.get("quote", ""))
        dn = str(verdict.get("doc", ""))
        verdict["quote_verified"] = bool(q) and dn in docs and norm(q) in norm(docs[dn])
        # a SUPPORTED verdict with an unverifiable quote is not support
        if verdict["verdict"] == "SUPPORTED" and not verdict["quote_verified"]:
            verdict["verdict"] = "NOT_FOUND"
            verdict["why"] = (verdict.get("why", "") + " [quote failed verbatim check]").strip()
        results.append({**c, **verdict})
    # OUT_OF_SCOPE = the issuer's filings can't adjudicate it (third-party filing, news,
    # market context) — needs a different source cite, but is NOT a gate failure.
    unresolved = [r for r in results if r["verdict"] in ("CONTRADICTED", "NOT_FOUND")]
    oos = [r for r in results if r["verdict"] == "OUT_OF_SCOPE"]
    report = {"memo": memo.name, "ticker": tk, "claims_source": how,
              "audited_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "dossier_built": json.loads((d / "manifest.json").read_text()).get("built"),
              "n_claims": len(results), "n_unresolved": len(unresolved),
              "n_out_of_scope": len(oos), "claims": results}
    out = memo.with_suffix(".audit.json")
    out.write_text(json.dumps(report, indent=1))
    for r in results:
        mark = {"SUPPORTED": "ok", "CONTRADICTED": "XX", "NOT_FOUND": "??", "OUT_OF_SCOPE": "--"}[r["verdict"]]
        print(f" [{mark}] ({r.get('type','fact')}) {r['claim'][:90]}")
        if r["verdict"] not in ("SUPPORTED", "OUT_OF_SCOPE"):
            print(f"      -> {r['verdict']}: {r.get('why','')} {('['+r.get('doc','')+'] '+r.get('quote',''))[:140]}")
    print(f"audit: {len(results)} claims, {len(unresolved)} unresolved, {len(oos)} out-of-scope -> {out.name}")
    return 1 if unresolved else 0


def status():
    for m in sorted(NAMES.glob("*/manifest.json")):
        j = json.loads(m.read_text())
        print(f"{j['ticker']:6s} built {j['built'][:16]}  {len(j['filings'])} filings  "
              f"{j.get('n_verified', 0)}/{j.get('n_terms', 0)} terms verified")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["build"] and len(a) >= 2:
        cik = a[a.index("--cik") + 1] if "--cik" in a else None
        build(a[1], cik)
    elif a[:1] == ["audit"] and len(a) >= 2:
        sys.exit(audit(a[1], a[2] if len(a) > 2 and not a[2].startswith("-") else None))
    elif a[:1] == ["status"]:
        status()
    else:
        sys.exit("usage: dossier.py build TICKER [--cik N] | audit MEMO.md [TICKER] | status")
