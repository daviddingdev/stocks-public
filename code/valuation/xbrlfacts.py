#!/usr/bin/env python3
"""Read the company's OWN filing, not just the SEC's summary API.

THE PROBLEM, in one sentence: the `companyfacts` API silently drops every number a
company files under a label it invented itself, and a dropped liability is
indistinguishable from no liability.

Companies file financials as tagged data. Most numbers use a standard dictionary
(`us-gaap`). Anything the dictionary does not cover, a company tags with its OWN label —
an "extension" in the `<ticker>:` namespace. `companyfacts` serves only the standard
dictionary. Measured 2026-08-18:

    ARI    370 `ari:`   facts in its 10-Q   ->  companyfacts serves: dei, ecd, us-gaap
    ARES   732 `ares:`  facts in its 10-Q   ->  companyfacts serves: dei, ecd, us-gaap
    FC      70 `fc:`    facts in its 10-Q   ->  companyfacts serves: dei, srt, us-gaap
    LBRDP   77 `lbrda:` facts in its 10-Q   ->  companyfacts serves: dei, us-gaap

Zero of those extension facts reach us. That is why ARI's fincard printed $1.24B of net
cash against a true $868M: its $371,428,000 of debt is filed as
`ari:DebtRelatedToRealEstateOwnedHeldForInvestment`, which the API does not carry. The COO
re-derived that number by hand on 2026-08-16. It was machine-readable the whole time — we
were reading the wrong endpoint.

The bias is one-directional and that is what makes it dangerous: an omitted liability
always makes a balance sheet look BETTER, so this failure manufactures apparent net-cash
bargains — precisely the screen output this book is built to act on.

This module fetches a filing's XBRL instance document (every fact the company filed,
extensions included) and finds candidates for a concept we could not resolve.

IT PROPOSES, IT DOES NOT SILENTLY ADOPT. ARI's 371,428,000 appears under three different
extension tags in the same filing; picking one blind would trade a visible flag for an
invisible wrong number, which is the worse failure. Candidates come back ranked, with the
tag and context, for a footing test or a human to confirm.

CLI:
    xbrlfacts.py <TICKER>                     list the extension namespaces + fact count
    xbrlfacts.py <TICKER> --find <keyword>    candidate facts matching a keyword
"""
import json
import re
import sys
from pathlib import Path
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edgar_identity import UA  # SEC contact identity, config-driven
SEC = "https://www.sec.gov"
DATA = "https://data.sec.gov"
STD_NS = {"us-gaap", "dei", "srt", "xbrldi", "xhtml", "ecd", "ffd", "invest",
          "country", "currency", "stpr", "link", "xlink", "xsi", "iso4217"}

_FACT = re.compile(r'<([\w.]+):([A-Za-z0-9_]+)([^>]*?)>([^<]*)</\1:\2>', re.S)
_CTX = re.compile(r'contextRef="([^"]+)"')
_UNIT = re.compile(r'unitRef="([^"]+)"')



# --------------------------------------------------------------------- contexts
# A fact on its own is not a number you can use — you also need WHEN it is for and WHAT
# it is for. Both live in its context. ARI's 371,428,000 appears four times in one filing;
# what separates the one we want from the three we don't is entirely here:
#
#   PERIOD      an instant of 2026-06-30 (this balance sheet) vs 2025-12-31 (the prior
#               column, same tag, printed right beside it)
#   DIMENSIONS  a context with explicitMember children is a SEGMENT or a sub-entity
#               breakdown. A consolidated balance-sheet line has NO dimensions. This is
#               what stops us adopting one property's debt as the company's debt.
#
# David 2026-08-18: "making sure every single agent understands... what time all the data
# comes from... I only want up to date data." Period matching is that rule, enforced here.
_CTX_BLOCK = re.compile(r'<(?:\w+:)?context[^>]*\sid="([^"]+)"(.*?)</(?:\w+:)?context>', re.S)
_INSTANT = re.compile(r'<(?:\w+:)?instant>\s*([\d-]+)\s*</(?:\w+:)?instant>')
_START = re.compile(r'<(?:\w+:)?startDate>\s*([\d-]+)\s*</(?:\w+:)?startDate>')
_END = re.compile(r'<(?:\w+:)?endDate>\s*([\d-]+)\s*</(?:\w+:)?endDate>')
_MEMBER = re.compile(r'explicitMember')


def contexts(raw):
    """{context_id: {instant|start|end, dimensional: bool}} for every context in a filing."""
    out = {}
    for cid, body in _CTX_BLOCK.findall(raw):
        inst = _INSTANT.search(body)
        st, en = _START.search(body), _END.search(body)
        out[cid] = {"instant": inst.group(1) if inst else None,
                    "start": st.group(1) if st else None,
                    "end": en.group(1) if en else None,
                    "dimensional": bool(_MEMBER.search(body))}
    return out


def _get(url, timeout=120):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def latest_filing(cik, forms=("10-Q", "10-K")):
    """(accession-no-dashes, form, date, primary-doc) for the most recent periodic report."""
    subs = json.loads(_get(f"{DATA}/submissions/CIK{int(cik):010d}.json"))
    r = subs["filings"]["recent"]
    for form, date, acc, doc in zip(r["form"], r["filingDate"], r["accessionNumber"],
                                    r["primaryDocument"]):
        if form in forms:
            return acc.replace("-", ""), form, date, doc
    return None, None, None, None


def instance_url(cik, acc):
    """The XBRL instance document inside a filing — the file that holds every fact.
    Named <ticker>-<period>_htm.xml in modern inline-XBRL filings."""
    idx = json.loads(_get(f"{SEC}/Archives/edgar/data/{int(cik)}/{acc}/index.json"))
    names = [i["name"] for i in idx["directory"]["item"]]
    for n in names:
        if n.endswith("_htm.xml"):
            return f"{SEC}/Archives/edgar/data/{int(cik)}/{acc}/{n}"
    return None


def facts(cik, acc=None):
    """Every fact in the filing, as dicts. Extensions included — that is the whole point."""
    if acc is None:
        acc, *_ = latest_filing(cik)
    if not acc:
        return []
    url = instance_url(cik, acc)
    if not url:
        return []
    raw = _get(url).decode("utf-8", "ignore")
    ctxs = contexts(raw)
    out = []
    for ns, tag, attrs, val in _FACT.findall(raw):
        v = val.strip()
        if not v:
            continue
        ctx = _CTX.search(attrs)
        unit = _UNIT.search(attrs)
        num = None
        if re.fullmatch(r"-?\d+(\.\d+)?", v.replace(",", "")):
            try:
                num = float(v.replace(",", ""))
            except ValueError:
                num = None
        cid = ctx.group(1) if ctx else ""
        c = ctxs.get(cid, {})
        out.append({"ns": ns, "tag": tag, "value": v, "number": num,
                    "context": cid, "unit": unit.group(1) if unit else "",
                    "instant": c.get("instant"), "start": c.get("start"), "end": c.get("end"),
                    "dimensional": c.get("dimensional", False)})
    return out


def namespaces(fs):
    from collections import Counter
    c = Counter(f["ns"] for f in fs)
    return {"all": dict(c), "extensions": {k: v for k, v in c.items() if k not in STD_NS}}


# Words that never name a balance-sheet line, however well they match the concept.
# ARI's filing carries `DebtInstrumentCovenantNetWorthThreshold` = $600,000,000, dated the
# same instant and consolidated — it passes every other filter and is not debt, it is the
# covenant it must not breach. Disclosure ABOUT a number is not the number.
NEGATIVE = ("covenant", "threshold", "fairvalue", "fair_value", "maximum", "minimum",
            "weightedaverage", "percentage", "rate", "ratio", "guarantee", "commitment",
            "unused", "available", "capacity", "pro_forma", "proforma", "restricted")


def find(fs, keywords, only_numeric=True, unit_hint="usd", exclude=NEGATIVE):
    """Candidate facts whose TAG NAME matches any keyword. Ranked by magnitude, because a
    balance-sheet line we are missing is usually the largest fact bearing that word."""
    kws = [k.lower() for k in (keywords if isinstance(keywords, (list, tuple)) else [keywords])]
    hits = []
    for f in fs:
        if f["ns"] in STD_NS and f["ns"] != "us-gaap":
            continue
        if only_numeric and f["number"] is None:
            continue
        if unit_hint and f["unit"] and unit_hint not in f["unit"].lower():
            continue
        name = f["tag"].lower()
        if exclude and any(x in name for x in exclude):
            continue
        if any(k in name for k in kws):
            hits.append(f)
    hits.sort(key=lambda f: -(abs(f["number"]) if f["number"] is not None else 0))
    return hits



def resolve_instant(fs, keywords, asof, require_consolidated=True):
    """Candidates for a BALANCE-SHEET concept as of a specific date.

    Two filters do nearly all the work, and both are the period discipline David asked for:
    the fact must be an instant dated exactly `asof` (not last quarter's column, which
    carries the same tag), and it must be consolidated (no dimensions — not one segment's
    or one property's slice of the number).

    Returns (unique_fact_or_None, all_candidates). A caller that gets None back has a
    genuine ambiguity and should FLAG with the candidates named, never guess among them."""
    cands = [f for f in find(fs, keywords)
             if f.get("instant") == asof and (not require_consolidated or not f["dimensional"])]
    vals = {f["number"] for f in cands}
    if len(vals) == 1 and cands:
        return cands[0], cands
    return None, cands


def _cli(argv):
    if not argv:
        print(__doc__)
        return 2
    tk = argv[0].upper()
    import pathlib
    card = pathlib.Path(__file__).resolve().parent.parent / "agent" / "names" / tk / "fincard.json"
    if not card.exists():
        print(f"no fincard for {tk} — need its CIK", file=sys.stderr)
        return 1
    cik = json.loads(card.read_text())["cik"]
    acc, form, date, _ = latest_filing(cik)
    fs = facts(cik, acc)
    ns = namespaces(fs)
    print(f"{tk} · {form} filed {date} · {len(fs):,} facts")
    print(f"  namespaces: {ns['all']}")
    print(f"  EXTENSIONS companyfacts does not serve: {ns['extensions'] or 'none'}")
    if "--find" in argv:
        kw = argv[argv.index("--find") + 1]
        hits = find(fs, kw)
        print(f"\n  candidates matching {kw!r}: {len(hits)}")
        for h in hits[:14]:
            per = h.get("instant") or f"{h.get('start')}..{h.get('end')}"
            dim = " [segment]" if h["dimensional"] else ""
            print(f"    {h['ns']}:{h['tag'][:46]:<46} {h['number']:>16,.0f}  {per}{dim}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
