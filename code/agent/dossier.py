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
FORM_COUNTS = {"10-K": 1, "10-Q": 2, "8-K": 4, "DEF 14A": 1, "DEFM14A": 1,
               "PREM14A": 1, "8-A12B": 1, "8-A12G": 1}
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
    for f in picked:
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}/{f['doc']}"
        try:
            txt = strip_html(get(url))
        except Exception as e:
            f["error"] = str(e)[:80]
            continue
        if len(txt) > 3_000_000:
            txt, f["truncated"] = txt[:3_000_000], True
        name = f"{f['date']}_{f['form'].replace(' ', '').replace('/', '')}.txt"
        (d / "filings" / name).write_text(txt)
        f["file"], f["chars"] = f"filings/{name}", len(txt)

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

    terms = extract_terms(d, title)
    (d / "terms.json").write_text(json.dumps(terms, indent=1))
    (d / "manifest.json").write_text(json.dumps(
        {"ticker": tk, "cik": cik, "resolved_via": via, "title": title,
         "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "filings": picked, "n_terms": len(terms.get("terms", [])),
         "n_verified": sum(1 for t in terms.get("terms", []) if t.get("verified"))}, indent=1))
    print(f"dossier {tk}: {len([f for f in picked if f.get('file')])} filings · "
          f"{len(facts)} fact series · {len(terms.get('terms', []))} terms "
          f"({sum(1 for t in terms.get('terms', []) if t.get('verified'))} quote-verified)")
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
        "equipment (capex); and debt_lt = the balance sheet's borrowings line, whatever it is "
        "called at this issuer — 'Long-term debt', 'Notes payable', 'Secured debt arrangements, "
        "net', 'Senior secured notes, net', 'Term loan', 'Debt related to real estate owned' — "
        "copying the SINGLE largest current-column debt line; if every debt line shows a dash or "
        "the balance sheet has none, debt_lt is NOT_SHOWN. "
        "JSON {\"cash\":\"<digits or NOT_SHOWN>\",\"cfo\":\"...\",\"capex\":"
        "\"...\",\"debt_lt\":\"...\",\"scale\":\"thousands|millions|units|unknown\"}\n\n"
        + "\n\n[---]\n\n".join(wins), num_predict=400)
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


def keyword_windows(txt, keywords, width=1200, cap=4, total_cap=14000):
    """Code locates candidate passages; the model only reads these."""
    spots = []
    low = txt.lower()
    for kw in keywords:
        for m in re.finditer(re.escape(kw), low):
            spots.append(m.start())
    spots.sort()
    windows, last_end = [], -1
    for s in spots:
        a, b = max(0, s - width // 2), min(len(txt), s + width)
        if a < last_end:          # merge overlapping windows
            windows[-1] = (windows[-1][0], b)
        else:
            windows.append((a, b))
        last_end = b
    windows = windows[:cap * 3]
    out, used = [], 0
    for a, b in windows:
        if used >= total_cap or len(out) >= cap * 2:
            break
        chunk = txt[a:b]
        out.append(chunk)
        used += len(chunk)
    return out


def extract_terms(d, title):
    """Local model extracts security/contract terms from keyword-located passages.
    Every extraction must carry a verbatim quote; code verifies the quote exists."""
    out = {"extracted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "model": f"{DEFAULT_MODEL} (local)", "terms": []}
    for doc in sorted((d / "filings").glob("*.txt")):
        txt = doc.read_text(errors="replace")
        wins = keyword_windows(txt, TERM_KEYWORDS)
        if not wins:
            continue
        excerpt = "\n\n[---]\n\n".join(wins)
        v = ask_json(
            f"These are excerpts from an SEC filing ({doc.name}) for {title}. Extract every "
            "explicit SECURITY or DEAL TERM present: redemption (optional/mandatory, dates, "
            "prices), conversion/exchange ratios, dividend rate & cumulative status, liquidation "
            "preference, change-of-control provisions, tender/dissolution/distribution terms, "
            "maturity/call dates. Return JSON {\"terms\":[{\"type\":\"...\",\"detail\":\"<one "
            "precise clause with numbers/dates>\",\"quote\":\"<supporting sentence copied "
            "CHARACTER-FOR-CHARACTER from the excerpt, max 40 words>\"}]}. Only terms explicitly "
            "in the text — omit anything you cannot quote. Empty list if none.\n\n" + excerpt,
            num_predict=1400)
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
    """Cheap retrieval: rank paragraphs by rare-token overlap with the claim."""
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
    for _, i in best[:n]:
        chunk = "\n\n".join(paras[max(0, i - 1):i + 2])[:width * 3]
        out.append(chunk)
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
        # 2) model path: retrieval across all docs, best-first
        cands = []
        for name, doc_txt in docs.items():
            for chunk in score_paragraphs(doc_txt, c["claim"]):
                cands.append((name, chunk))
        cands = cands[:6]
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
                "CONTRADICTED, not NOT_FOUND.", num_predict=500)
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
