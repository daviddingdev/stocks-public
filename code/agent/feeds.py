#!/usr/bin/env python3
"""
Agent intel feeds — news + event connectors for the BrokerB agentic account.

Pulls, for every ticker in universe.txt:
  - Finnhub company news (last 7 days)
  - SEC EDGAR recent filings (8-K / 10-K / 10-Q / Form 4 / 13D-G, last 45 days)
plus Finnhub general market news and the Finnhub earnings calendar (next 21 days,
filtered to the universe). Everything lands in data/feed.json — read by the agent's
trading loop (as decision context) and by the dashboard /agent page (as display).

No Claude tokens involved — pure Python, cron-friendly, free APIs only.
CLI: feeds.py refresh
"""
import datetime as dt
import json
import pathlib
import os
import re
import sys
import time
from pathlib import Path

def _write_json(path, obj, indent=1):
    """Atomic write. feeds/triggers/mcp_sync all rewrite these files on cron while the
    PM session and the dashboard are reading them — feed.json in particular is written
    by the :00 feed refresh at the same minute the trade session starts. A plain
    write_text truncates first, so a reader can catch an empty or half-written file and
    conclude the world is empty. Same-dir temp + os.replace makes the swap atomic."""
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent))
    os.replace(tmp, path)


import requests

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
DATA = HERE / "data"
CONF = ENGINE / "config"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # SEC contact identity, config-driven

FUNNEL_LOG = DATA / "funnel_counts.jsonl"

def funnel_record(stage, n_in, n_out, **extra):
    """scout.py-172 (funnel audit 2026-09-07): the funnel had no memory of its own
    counts, so STRATEGY-PROPOSAL-v3 §7's quota (>=10 leads -> >=5 shelves -> >=3 gate
    passes -> >=2 cards -> 1-2 teardowns) was unmeasurable — a week that missed it looked
    identical to a week that hit it. One append-only row per stage per run: what went IN,
    what came OUT, when. `extra` carries anything stage-specific worth keeping (e.g. a
    per-channel breakdown) without forcing every stage into the same two numbers."""
    row = {"stage": stage, "in": n_in, "out": n_out,
           "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **extra}
    with FUNNEL_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def universe():
    """Agent universe = its own watchlist + its own positions. INDEPENDENCE
    (David, 2026-08-12): BROKERA holdings are no longer merged in — this book is
    its own fund; BROKERA looks at it, not the other way around. (triggers.py
    still watches BROKERA names for David's phone via its own direct queries.)"""
    f = HERE / "universe.txt"
    if not f.exists():
        return []
    # feeds.py-188: strip an inline '#' comment before treating the rest of the line
    # as a ticker — a line like "CNDT  # note..." used to fail isalpha() as a whole
    # and silently drop CNDT rather than just the note.
    base = [l.split("#", 1)[0].strip().upper() for l in f.read_text().splitlines()]
    base = [l for l in base if l]
    try:
        base += [p2["symbol"].upper() for p2 in
                 json.loads((DATA / "portfolio.json").read_text()).get("positions", []) if p2.get("symbol")]
    except Exception:
        pass
    return [t for t in dict.fromkeys(base) if t.isalpha()]


def fh_key():
    try:
        return json.loads((CONF / "keys.json").read_text()).get("finnhub", "")
    except Exception:
        return ""


FH_FAILS = []

def fh_get(path, **params):
    params["token"] = fh_key()
    try:
        r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        # a silent None here is how an empty section ends up stamped "fresh" — say it
        FH_FAILS.append(f"{path} HTTP {r.status_code}")
        print(f"  ! finnhub {path} -> HTTP {r.status_code}", file=sys.stderr)
        return None
    except Exception as e:
        FH_FAILS.append(f"{path} {type(e).__name__}")
        print(f"  ! finnhub {path} -> {type(e).__name__}", file=sys.stderr)
        return None


def _iso(unix):
    """Unix epoch -> unambiguous '2026-08-12T14:00:00Z' (models misread bare epochs
    and year-less dates; every feed timestamp carries year + explicit UTC)."""
    try:
        return dt.datetime.fromtimestamp(int(unix), dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    except Exception:
        return None


def company_news(tickers, days=7):
    to, frm = dt.date.today(), dt.date.today() - dt.timedelta(days=days)
    out = {}
    for tk in tickers:
        rows = fh_get("company-news", symbol=tk, **{"from": frm.isoformat(), "to": to.isoformat()}) or []
        out[tk] = [{"datetime": n.get("datetime"), "dt_utc": _iso(n.get("datetime")),
                    "headline": n.get("headline", ""),
                    "source": n.get("source", ""), "url": n.get("url", ""),
                    "summary": (n.get("summary") or "")[:400]} for n in rows[:12]]
        time.sleep(1.1)  # free tier: 60 req/min
    return out


def market_news(n=20):
    rows = fh_get("news", category="general") or []
    return [{"datetime": x.get("datetime"), "dt_utc": _iso(x.get("datetime")),
             "headline": x.get("headline", ""),
             "source": x.get("source", ""), "url": x.get("url", "")} for x in rows[:n]]


def earnings_calendar(tickers, days=21):
    # Per-symbol queries: the bulk endpoint caps at 1500 rows and silently drops
    # names outside its slice (bit us 2026-07-28 — feed showed 0 earnings while
    # HALO reported Aug 6). One request per ticker is reliable.
    frm, to = dt.date.today(), dt.date.today() + dt.timedelta(days=days)
    out = []
    for tk in tickers:
        resp = fh_get("calendar/earnings", symbol=tk, **{"from": frm.isoformat(), "to": to.isoformat()})
        for r in (resp or {}).get("earningsCalendar", []):
            out.append({"symbol": r.get("symbol"), "date": r.get("date"), "hour": r.get("hour", ""),
                        "epsEstimate": r.get("epsEstimate"), "revenueEstimate": r.get("revenueEstimate")})
        time.sleep(1.1)
    return out


# ---------- EDGAR ----------
def _load_cik_cache(cache):
    # feeds.py-193b (signals, 2026-09-11): cik_map() is called from feeds/scout/bench/vp/
    # triggers, several of them concurrently around the same cron minute, and the plain
    # write_text() below used to truncate-then-write -- a reader landing mid-write got a
    # truncated JSON fragment. On 2026-09-09 that crashed triggers.py market-hours (`m =
    # cik_map(); m.get(tk)` raised AttributeError: 'int' object has no attribute 'get'),
    # skipping every rule for that 5-min tick. A malformed cache is now treated the same
    # as a missing one -- refetch rather than hand a non-dict to every caller.
    try:
        v = json.loads(cache.read_text())
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def cik_map():
    """ticker -> zero-padded CIK, cached a week."""
    cache = DATA / "cik_map.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        cached = _load_cik_cache(cache)
        if cached is not None:
            return cached
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=UA, timeout=30)
        m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}
    except Exception:
        return _load_cik_cache(cache) or {}
    _write_json(cache, m)
    return m


# EDGAR's Dec-2024 modernization renamed the submissions.json form label for Schedule 13D/G
# from "SC 13D"/"SC 13G" to "SCHEDULE 13D"/"SCHEDULE 13G" (same rename noted below for the
# daily-index radar). Confirmed against CIK0000320121 (TLS): its 2026-07-24 SCHEDULE 13D used
# the new label and was silently dropped by this filter — the per-name pull (45-day window,
# would have caught it) never saw a single 13D/13G across the whole universe (feeds.py-011).
# Merger/liquidation proxy statements were missing entirely (triggers.py-040, 2026-08-25):
# ARI's DEFM14A — carrying the actual Special Meeting date, record date, and Initial Cash
# Distribution timing for a live agent-book liquidation thesis — filed 2026-08-24 and never
# reached feed.json or the filing-day trigger because no *14A form was in this set.
# 8-K/A and DEFA14A added (triggers.py-054, 2026-08-26): ARI faces contested litigation
# (attachment hearing 2026-09-25, 4 days before the 2026-09-29 vote) that can amend or
# supplement the DEFM14A's disclosures — those updates are the two standard EDGAR forms for
# "the 8-K/DEFM14A I already filed needs a correction or supplemental disclosure", most often
# used industry-wide for exactly this kind of merger-litigation development.
INTERESTING = {"8-K", "8-K/A", "10-K", "10-Q", "4", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A",
               "SCHEDULE 13D", "SCHEDULE 13G", "SCHEDULE 13D/A", "SCHEDULE 13G/A", "S-1", "424B5",
               "DEFM14A", "DEF 14A", "PREM14A", "PRE 14A", "DEFA14A"}


def edgar_filings(tickers, days=45):
    m = cik_map()
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {}
    for tk in tickers:
        cik = m.get(tk)
        if not cik:
            out[tk] = []
            continue
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=UA, timeout=30)
            rec = r.json()["filings"]["recent"]
        except Exception:
            out[tk] = []
            continue
        rows = []
        for form, date, acc, doc, items in zip(rec["form"], rec["filingDate"], rec["accessionNumber"],
                                               rec["primaryDocument"], rec.get("items", [""] * len(rec["form"]))):
            if date < cutoff or form not in INTERESTING:
                continue
            row = {"date": date, "form": form, "items": items,
                   "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"}
            if form == "4":
                # primaryDocument points at the XSLT-rendered viewer (xslF345X06/<file>.xml,
                # HTML under an .xml name); the raw ownershipDocument XML this parses lives at
                # the SAME filename one directory up, at the accession root (verified against
                # TLS 0001628280-26-058627 and LYFT 0001675948-26-000006, feeds.py-070).
                row["_xml_url"] = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                                   f"{acc.replace('-', '')}/{doc.rsplit('/', 1)[-1]}")
            rows.append(row)
        out[tk] = rows[:15]
        time.sleep(0.15)
    return _resolve_form4_transactions(out)


def _f4_val(container_tag, block):
    """The <value> immediately inside a Form 4 wrapper tag, e.g. <transactionShares><value>."""
    m = re.search(rf"<{container_tag}>(.*?)</{container_tag}>", block, re.S)
    if not m:
        return None
    vm = re.search(r"<value>\s*([^<]*?)\s*</value>", m.group(1), re.S)
    val = vm.group(1).strip() if vm else ""
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return val


def _f4_ownership_nature(txn_block):
    """Direct ('D') vs indirect ('I') ownership plus, for I, the natureOfOwnership footnote
    text (e.g. "By spouse's IRA") -- a Form 4 sale out of a family/trust account and a sale
    from the insider's own direct holding are opposite signals and were previously collapsed
    into one unlabeled number (feeds.py-073)."""
    m = re.search(r"<directOrIndirectOwnership>(.*?)</directOrIndirectOwnership>", txn_block, re.S)
    if not m:
        return None, None
    vm = re.search(r"<value>\s*([^<]*?)\s*</value>", m.group(1), re.S)
    nature = vm.group(1).strip() if vm else None
    note = None
    if nature == "I":
        nm = re.search(r"<natureOfOwnership>(.*?)</natureOfOwnership>", txn_block, re.S)
        if nm:
            nvm = re.search(r"<value>\s*([^<]*?)\s*</value>", nm.group(1), re.S)
            note = nvm.group(1).strip() if nvm else None
    return nature, note


def _f4_owner_role(owner_block):
    title_m = re.search(r"<officerTitle>\s*([^<]+?)\s*</officerTitle>", owner_block)
    if title_m and title_m.group(1).strip():
        return title_m.group(1).strip()

    def flag(tag):
        return bool(re.search(rf"<{tag}>\s*(1|true)\s*</{tag}>", owner_block, re.I))
    if flag("isOfficer"):
        return "Officer"
    if flag("isDirector"):
        return "Director"
    if flag("isTenPercentOwner"):
        return "10% Owner"
    return ""


# feeds.py-193 (hunt, 2026-09-10, ask journal/ops-155). The SUMMARY fields promoted onto a
# Form 4 row used to be computed inline inside _parse_form4_xml, which meant they could only
# ever be computed at PARSE time -- and form4_transactions.json is keyed by accession and
# cached FOREVER (a Form 4 is a point-in-time report). Every fix to this derivation therefore
# applied only to accessions first seen after the fix, and silently skipped the whole existing
# corpus: on 2026-09-10 that was 12 of 142 entries with no `transaction_date` at all (pre-077),
# 9 of them also promoting a `shares_after` that is not the filing's largest remaining balance
# (pre-073), and 12 whose transactions[] carry no `ownership_nature` (pre-076). Nothing
# reported it; a proof script exiting 1 for 13 days was the only signal.
#
# So the derivation lives here, is pure (transactions[] in, summary out, no network), and is
# stamped with F4_SUMMARY_SCHEMA. _resolve_form4_transactions re-derives any cached entry
# below the current stamp, so the NEXT fix to this function repairs the corpus by itself.
F4_SUMMARY_SCHEMA = 2


def _summarize_form4(txns):
    """Promote the whole filing's transactions[] to the summary fields diffbrief.py renders.
    Pure and offline: everything below is derived from txns alone.

    Every field is read with .get(): transactions[] parsed before feeds.py-076/077 carry
    only {kind, transaction_code, shares, price, shares_after, rule_10b5_1,
    rule_10b5_1_note} -- no `date`, no `ownership_nature`, no `price_note`. A re-derivation
    over the cached corpus has to survive that shape, and t["date"] raised KeyError on 12 of
    142 entries. Absent fields summarise to None, which is honest: the trade date was never
    captured for those filings and cannot be invented from what was."""
    summary = {}
    # Summary fields are what diffbrief.py renders -- they must reflect the WHOLE filing,
    # not transactions[0] (feeds.py-073, the ARI case: two non-derivative sales, 835 sh
    # out of a spouse's IRA to zero plus 125 sh direct, previously promoted as a single
    # 835-sh sale to zero). Group by code so a mixed filing (e.g. an S alongside a G gift)
    # aggregates within its own kind rather than blending unrelated transaction types.
    by_code = {}
    for t in txns:
        by_code.setdefault(t.get("transaction_code"), []).append(t)
    primary_code, primary_group = max(by_code.items(),
                                      key=lambda kv: sum(x.get("shares") or 0 for x in kv[1]))
    total_shares = sum(t.get("shares") or 0 for t in primary_group)
    priced = [t for t in primary_group if t.get("price")]
    wshares = sum(t.get("shares") or 0 for t in priced)
    price = (sum((t.get("price") or 0) * (t.get("shares") or 0) for t in priced) / wshares
             if wshares else (priced[0].get("price") if priced else None))
    dates = [t.get("date") for t in txns if t.get("date")]
    wavg_notes = [t.get("price_note") for t in primary_group if t.get("price_note")]
    plan_notes = [t.get("rule_10b5_1_note") for t in primary_group if t.get("rule_10b5_1_note")]
    # feeds.py-193 (hunt -> signals, ask signals-193, 2026-09-11): shares_after is NOT
    # max() across every transaction on the filing (feeds.py-073) -- that conflates
    # separate ownership accounts (an insider's smaller account overstates nothing, but
    # picks the WRONG account as "the" holding), returns a mid-sequence balance instead
    # of the closing one on a multi-transaction single account, and can promote a
    # DERIVATIVE count (options/RSUs, Table II) as a common-share count. The filing lists
    # Table I rows in document order, so the closing balance per account is that
    # account's LAST non-derivative transaction; summed across accounts gives the whole
    # common holding. This still satisfies the ARI case max() was introduced for
    # (feeds.py-073: 0 in the spouse's IRA + 162,417 direct = 162,417). Derivative
    # securities remaining is a different quantity and gets its own field rather than
    # being blended into a common-share count.
    nd = [t for t in txns if t.get("kind") == "non-derivative" and t.get("shares_after") is not None]
    dv = [t for t in txns if t.get("kind") == "derivative" and t.get("shares_after") is not None]
    nd_accounts = {}
    for t in nd:
        nd_accounts.setdefault((t.get("ownership_nature"), t.get("ownership_nature_note")), []).append(t)
    summary.update({
        "transaction_code": primary_code,
        "shares": total_shares,
        "price": price,
        "shares_after": (sum(g[-1]["shares_after"] for g in nd_accounts.values())
                         if nd_accounts else None),
        "derivative_shares_after": dv[-1]["shares_after"] if dv else None,
        # feeds.py-077/diffbrief.py-077 (coo, 2026-08-29): NOT "date" -- the filing row
        # this dict gets r.update()'d onto (edgar_filings' `row["date"]`) already means the
        # EDGAR index/FILED date. A same-named key here clobbered it with the <transaction
        # Date> (the TRADE date), collapsing two distinct dates into one with no way to
        # recover the other, and silently flipped the meaning of "date" for every consumer
        # (diffbrief.py's 14-day lookback, "Filings dated X or later", the since-last-
        # session filter) with no code change on their end. Kept under its own key, same
        # convention as transaction_code/price_is_weighted_avg.
        "transaction_date": max(dates) if dates else None,
        "rule_10b5_1": any(t.get("rule_10b5_1") for t in primary_group),
        "rule_10b5_1_note": plan_notes[0][:200] if plan_notes else None,
        "price_is_weighted_avg": len(priced) > 1 or any(t.get("price_is_weighted_avg")
                                                        for t in primary_group),
        "price_note": wavg_notes[0][:200] if wavg_notes else None,
    })
    summary["transactions"] = txns
    return summary


def _parse_form4_xml(xml_text):
    """Extract the fields BOOK.md's insider tripwires actually test (filer, transaction
    code, shares, price, post-transaction share count, Rule 10b5-1 status) from a Form 4's
    raw ownershipDocument XML. Before this the row carried only {date, form, items, url} --
    every field a tripwire needs was one fetch away and none of them were in feed.json
    (feeds.py-070). Schema verified live against TLS 0001628280-26-058627 (Dockery, code S,
    no plan) and LYFT 0001675948-26-000006 (Brewer CFO, code S, <aff10b5One>1</aff10b5One>
    plus an F1 footnote naming the 2026-03-13 plan date)."""
    owner_m = re.search(r"<reportingOwner>(.*?)</reportingOwner>", xml_text, re.S)
    owner_block = owner_m.group(1) if owner_m else ""
    name_m = re.search(r"<rptOwnerName>\s*([^<]+?)\s*</rptOwnerName>", owner_block)
    out = {"filer": name_m.group(1).strip() if name_m else "",
           "owner_title": _f4_owner_role(owner_block)}

    footnotes = {fm.group(1): fm.group(2).strip() for fm in
                 re.finditer(r'<footnote id="([^"]+)">(.*?)</footnote>', xml_text, re.S)}
    # the 2023 rule change added a document-level "filed pursuant to a Rule 10b5-1(c) plan"
    # checkbox (aff10b5One) -- newer filings (LYFT) set it directly; older-schema filings
    # only say so in a footnote text, so both are checked.
    doc_10b5_1 = bool(re.search(r"<aff10b5One>\s*(1|true)\s*</aff10b5One>", xml_text, re.I))

    txns = []
    for kind, block_re in (("non-derivative", r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>"),
                            ("derivative", r"<derivativeTransaction>(.*?)</derivativeTransaction>")):
        for tm in re.finditer(block_re, xml_text, re.S):
            b = tm.group(1)
            code_m = re.search(r"<transactionCode>\s*([^<]+?)\s*</transactionCode>", b)
            fn_ids = re.findall(r'footnoteId\s+id="([^"]+)"', b)
            fn_texts = [footnotes[i] for i in fn_ids if i in footnotes]
            plan_texts = [t for t in fn_texts if "10b5-1" in t]  # a transaction can carry
            # several footnotes (e.g. LYFT's plan-date note AND its weighted-avg-price note
            # on the SAME transaction) -- keep only the one that actually says 10b5-1.
            is_10b5_1 = doc_10b5_1 or bool(plan_texts)
            wavg_texts = [t for t in fn_texts if "weighted average" in t.lower()]
            nature, nature_note = _f4_ownership_nature(b)
            txns.append({"kind": kind,
                        "transaction_code": code_m.group(1).strip() if code_m else None,
                        "date": _f4_val("transactionDate", b),
                        "shares": _f4_val("transactionShares", b),
                        "price": _f4_val("transactionPricePerShare", b),
                        "shares_after": _f4_val("sharesOwnedFollowingTransaction", b),
                        "ownership_nature": nature,
                        "ownership_nature_note": nature_note,
                        "rule_10b5_1": is_10b5_1,
                        "rule_10b5_1_note": plan_texts[0][:200] if plan_texts else None,
                        "price_is_weighted_avg": bool(wavg_texts),
                        "price_note": wavg_texts[0][:200] if wavg_texts else None})
    if txns:
        out.update(_summarize_form4(txns))
    else:
        # feeds.py-140 (coo, 2026-09-05): a filing that parsed cleanly but has no
        # non-derivative/derivative transactions (e.g. QVCG/Barclays 0000312069-26-058627,
        # a 10% owner reporting only that it fell below the threshold) looked IDENTICAL in
        # feed.json to one _parse_form4_xml couldn't read at all -- both left every
        # transaction field absent, and a reader had no way to tell "nothing happened" from
        # "the parser failed". <remarks> is where Section 16 filers explain a transaction-
        # free Form 4 (a plan expiring, a 10% stake unwound without a pecuniary change); kept
        # verbatim when present so the reason is the filer's own words, not a guess.
        remarks_m = re.search(r"<remarks>\s*(.*?)\s*</remarks>", xml_text, re.S)
        remarks = re.sub(r"\s+", " ", remarks_m.group(1)).strip() if remarks_m else ""
        out["no_transactions_reason"] = remarks[:300] if remarks else "no non-derivative or derivative transactions in filing"
    return out


def _resummarize_cache(cache):
    """Re-derive the summary of every cached entry written before the current
    F4_SUMMARY_SCHEMA, in place. Returns the list of repaired accessions.

    THE DEFECT THIS EXISTS FOR (hunt, 2026-09-10, ask journal/ops-155):
    form4_transactions.json is keyed by accession and cached forever, so a fix to the
    summary derivation only ever reached filings first seen AFTER the fix. Measured on
    2026-09-10, before this ran: of 142 entries, 9 promoted a `shares_after` that was not
    the filing's largest remaining balance (the feeds.py-073 defect, fixed 2026-08-28 and
    still live in the cache 13 days later) and 12 had no `transaction_date` (feeds.py-077).
    The residue had been read as unreachable because those accessions had aged out of
    feed.json's window -- but reach is not repairability: the summary is a pure function of
    transactions[], so 9 of the 9 were fixable offline with no EDGAR fetch at all.

    Re-derivation is offline and idempotent: verified against the live corpus, all 123
    entries already at the current schema re-derive byte-identically. What it CANNOT repair
    is a field the parser never captured -- pre-076/077 transactions[] have no `date`, so
    `transaction_date` stays None for those and the stamp records why."""
    repaired = []
    for acc, rec in cache.items():
        if not isinstance(rec, dict) or rec.get("_summary_schema") == F4_SUMMARY_SCHEMA:
            continue
        txns = rec.get("transactions")
        if txns:
            before = {k: rec.get(k) for k in ("transaction_code", "shares", "price",
                                              "shares_after", "transaction_date")}
            rec.update(_summarize_form4(txns))
            if any(rec.get(k) != v for k, v in before.items()):
                repaired.append(acc)
        rec["_summary_schema"] = F4_SUMMARY_SCHEMA
    return repaired


def _resolve_form4_transactions(filings, cap=30):
    """Form 4 rows land in feed.json shaped like every other filing -- but BOOK.md's
    insider tripwires test transaction fields that only live inside the filing's own
    XML, one fetch away and never taken (feeds.py-070, repro: 13 held-name Form 4 rows
    with zero transaction fields). Cached forever by accession: a Form 4 is a
    point-in-time report and this codebase never sees the amended form "4/A" (not in
    INTERESTING). Cap + carry-the-backlog matches _resolve_13d_subjects/_resolve_spin_parents."""
    cache_f = DATA / "form4_transactions.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        cache = {}
    _resummarize_cache(cache)
    fetched = 0
    for rows in filings.values():
        for r in rows:
            if r.get("form") != "4":
                continue
            xml_url = r.pop("_xml_url", None)
            if not xml_url:
                continue
            acc = xml_url.rsplit("/", 2)[-2]
            if acc in cache:
                r.update(cache[acc])
                continue
            if fetched >= cap:  # politeness: resolve the backlog across successive runs
                continue
            fetched += 1
            try:
                xml_text = requests.get(xml_url, headers=UA, timeout=30).text
                entry = _parse_form4_xml(xml_text)
            except Exception as e:
                # feeds.py-140: a genuine parse failure must not look like the
                # "no transactions" case _parse_form4_xml records above -- both used to
                # leave the row with zero transaction fields and nothing to tell them apart.
                entry = {"parse_error": str(e)[:200]}
            time.sleep(0.15)
            entry["_summary_schema"] = F4_SUMMARY_SCHEMA
            cache[acc] = entry
            r.update(entry)
    cache_f.write_text(json.dumps(cache, indent=1))
    return filings


def form4_day_details(cik, acc_doc_pairs):
    """Parse (or pull from the accession-keyed cache) every Form 4 an issuer filed on one
    day, for classify_form4_entries to bucket. Same cache file/key convention as
    _resolve_form4_transactions (accession with dashes stripped) so a filing fetched here
    is never re-fetched there, or vice versa."""
    cache_f = DATA / "form4_transactions.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        cache = {}
    # Same self-healing pass as _resolve_form4_transactions: this reader shares the cache
    # file, so an entry repaired on one path must not be handed back stale on the other.
    dirty = bool(_resummarize_cache(cache))
    out = []
    for acc, doc in acc_doc_pairs:
        acc_nodash = acc.replace("-", "")
        entry = cache.get(acc_nodash)
        if entry is None:
            xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc.rsplit('/', 1)[-1]}"
            try:
                entry = _parse_form4_xml(requests.get(xml_url, headers=UA, timeout=30).text)
            except Exception as e:
                entry = {"parse_error": str(e)[:200]}
            entry["_summary_schema"] = F4_SUMMARY_SCHEMA
            cache[acc_nodash] = entry
            dirty = True
            time.sleep(0.15)
        out.append(entry)
    if dirty:
        cache_f.write_text(json.dumps(cache, indent=1))
    return out


def classify_form4_entries(entries):
    """Bucket one issuer-day's parsed Form 4s into open-market (code P/S, priced) vs
    grant/tax (code A/M/F) per feeds.py-124 -- a spin-off's identical all-grant Form 4s
    (MBGL 2026-09-02: 8 filings, all code A, $0 RSU grants under the LTIP) should read as
    ONE line saying so, not eight copies of 'filed 4 today'. Codes outside both sets (gifts,
    conversions, ...) land in 'other' and count toward N but neither bucket."""
    open_market, grants, other = [], [], []
    net_dollars = 0.0
    for e in entries:
        code = e.get("transaction_code")
        shares = e.get("shares") or 0
        price = e.get("price") or 0
        if code in ("P", "S") and price:
            open_market.append(e)
            net_dollars += shares * price * (1 if code == "P" else -1)
        elif code in ("A", "M", "F"):
            grants.append(e)
        else:
            other.append(e)
    return {"open_market": open_market, "grants": grants, "other": other, "net_dollars": net_dollars}


_DATED_FORM_RE = re.compile(
    r"\b(SC 13D/A|SC 13G/A|SC 13D|SC 13G|8-K/A|8-K|10-K|10-Q|DEFM14A|DEF 14A|424B5)\b")


def resolve_dated_form_expectations():
    """feeds.py-128 (PM, delegated 2026-09-01): a dates.json item that names an expected
    SEC form and a date is a lookup, not judgment -- the PM did this by hand for ARI's
    Brooklyn 8-K window on 2026-09-04 (curl the submissions JSON, read off form/filingDate).
    Once an item's date arrives, check EDGAR for the issuer (ticker read off the item's
    leading token in 'what', the desk's own convention: 'ARI Brooklyn...', 'ETD $3.00/sh
    ...', 'MBGL:...') and write the outcome onto the item so the next session reads a fact
    instead of re-deriving it. Idempotent via '_form_resolved'; returns the resolved lines
    for session_brief.md's Watching section."""
    path = DATA / "dates.json"
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return []
    items = doc.get("items") or []
    m_cmap = cik_map()
    today = dt.date.today().isoformat()
    resolved = []
    changed = False
    for it in items:
        if it.get("_form_resolved") or it.get("date", "") > today:
            continue
        what = it.get("what", "")
        # 'what' only, not 'expect' -- 'expect' regularly cites a form ALREADY on file as
        # evidence ("The 10-Q filed 2026-08-10 says...") and that read as a pending
        # expectation for the item's OWN date (ARI's 2026-08-31 item, false-positive
        # caught in review before this shipped). 'what' is the short label the desk
        # itself wrote for what this date IS, so a form named there names what's due.
        form_m = _DATED_FORM_RE.search(what)
        if form_m and what[form_m.end():form_m.end() + 15].strip().lower().startswith("filed"):
            form_m = None  # "10-Q filed 2026-08-07" in the label itself is also evidence, not a due date
        tk_m = re.match(r"\s*([A-Z]{2,6})\b", what)
        if not form_m or not tk_m or tk_m.group(1) not in m_cmap:
            continue
        tk, form, cik = tk_m.group(1), form_m.group(1), m_cmap[tk_m.group(1)]
        try:
            rec = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                               headers=UA, timeout=20).json()["filings"]["recent"]
        except Exception:
            continue
        hit = next(((f, d, a) for f, d, a in
                    zip(rec["form"], rec["filingDate"], rec["accessionNumber"])
                    if f == form and d >= it["date"]), None)
        if hit:
            outcome = f"filed {hit[0]} {hit[1]} (acc {hit[2]})"
        else:
            last = next(((f, d) for f, d in zip(rec["form"], rec["filingDate"])), (None, None))
            outcome = f"no {form} by {it['date']}; last filing {last[0] or '?'} {last[1] or '?'}"
        it["expect"] = f"{it.get('expect', '')} RESOLVED {today}: {outcome}."
        it["_form_resolved"] = today
        resolved.append(f"{tk}: {outcome} (was watching for {form} by {it['date']})")
        changed = True
        time.sleep(0.15)
    if changed:
        _write_json(path, doc)
    return resolved


# ---------- special-situations radar (market-wide, not universe-bound) ----------
# Sourcing doctrine: _engine/research/SOURCING.md — mechanism-driven channels.
# SC 13D = fresh activist/concentrated stakes · 10-12B = spinoff registrations ·
# Form 25 = delistings (forced-selling flag). Parsed from EDGAR daily form indices.
# NB: EDGAR's 13D/G modernization (Dec 2024) renamed the index form to "SCHEDULE 13D";
# "SC 13D" kept for any legacy stragglers. Amendments (/A) are excluded on purpose —
# we want NEW stakes, not position updates.
RADAR_FORMS = {"SCHEDULE 13D": "sc13d", "SC 13D": "sc13d",
               "10-12B": "spins", "25": "delistings", "25-NSE": "delistings",
               "EFFECT": "reg_effective",
               # build-003: N-14 registers a fund merger/reorganization (including a
               # mutual-fund/CEF converting into an ETF share class) -- both index labels
               # seen live (11 trading days: 12x "N-14", 1x "N-14 8C"); "N-14 8C/A" excluded
               # as an amendment, same convention as SC 13D/A.
               "N-14": "n14", "N-14 8C": "n14"}


def _parse_idx_line(line):
    for form in RADAR_FORMS:
        if line.startswith(form + " ") or line.startswith(form + "/A "):
            parts = line.split()
            if len(parts) < 4:
                return None
            ntok = len(form.split())
            actual = " ".join(parts[:ntok])
            if actual not in RADAR_FORMS:  # excludes amendments like SC 13D/A
                return None
            return {"form": actual, "company": " ".join(parts[ntok:-3]),
                    "cik": parts[-3], "date": parts[-2],
                    "url": "https://www.sec.gov/Archives/" + parts[-1]}
    return None


# scout.py-061: a 13D's own Item 5 percentage basis reads "based upon 143,044,372.357
# shares of common stock outstanding as of ..." — FRACTIONAL SHARES TO THREE DECIMALS —
# on an interval/tender-offer fund (repurchases at NAV, no listed price, nothing for an
# activist to force). A listed issuer's share count is always a whole number. Cheap and
# structural: catch it once here, at the same fetch that already resolves filer/subject,
# rather than let every downstream stage re-discover the same disqualifying fact.
_FRAC_SHARES_RE = re.compile(r"\b\d[\d,]{2,}\.\d+\s+shares\s+of\b", re.I)


def _resolve_13d_subjects(rows, cap=20):
    """The daily form index lists an accession under BOTH the filer's CIK and the
    subject's CIK (EDGAR indexes a 13D against every party named in it); this code used
    to keep whichever row the dedup pass happened to see first, so ~44% of rows ended up
    with the SUBJECT's name doing double duty as the filer — 'issuer files 13D on itself',
    which Rule 13d-1 makes impossible (feeds.py-049). The full-submission header carries
    separate FILED BY and SUBJECT COMPANY blocks with their own CIKs: fetch it once per
    accession, cache forever, and trust neither field to the daily index."""
    cache_f = DATA / "sc13d_subjects.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        cache = {}
    t_by_cik = {int(c): t for t, c in cik_map().items()}
    fetched = 0
    for r in rows:
        acc = r["url"].rsplit("/", 1)[-1]
        cached = cache.get(acc)
        if cached and "filer" in cached:  # stale entries pre-feeds.py-049 lack "filer" — re-resolve them
            r.update(cached)
            continue
        if fetched >= cap:  # politeness: resolve the backlog across successive runs
            if cached:
                r.update(cached)
            continue
        entry = dict(cached or {})
        try:
            # 50KB covers the SGML header plus the cover page / Item 5 of a typical 13D —
            # the 6KB used for filer/subject resolution alone stops before Item 5 ever starts.
            full = requests.get(r["url"], headers=UA, timeout=30).text[:50_000]
            head = full[:6000]
            fetched += 1
            time.sleep(0.15)
            m = re.search(r"SUBJECT COMPANY:.*?COMPANY CONFORMED NAME:\s*(.+?)\n.*?CENTRAL INDEX KEY:\s*(\d+)",
                          head, re.S)
            if m:
                subj_cik = int(m.group(2))
                entry["subject"] = m.group(1).strip()[:60]
                entry["subject_ticker"] = t_by_cik.get(subj_cik)
            fm = re.search(r"FILED BY:.*?COMPANY CONFORMED NAME:\s*(.+?)\n", head, re.S)
            if fm:
                entry["filer"] = fm.group(1).strip()[:80]
            entry["frac_shares_basis"] = bool(_FRAC_SHARES_RE.search(full))
        except Exception:
            pass
        cache[acc] = entry
        r.update(entry)
    cache_f.write_text(json.dumps(cache, indent=1))
    return rows


def _sec_company_map():
    """title/ticker rows from the SEC's own company_tickers.json, cached a week. Same
    cache file scout.py already writes/reads (sec_company_tickers.json) — sharing it
    means the two collectors cooperate on one fetch instead of duplicating it."""
    cache = DATA / "sec_company_tickers.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=UA, timeout=30)
        data = r.json()
        cache.write_text(json.dumps(data))
        return data
    except Exception:
        return json.loads(cache.read_text()) if cache.exists() else {}


def _name_to_ticker(name):
    data = _sec_company_map()
    want = re.sub(r"[^a-z0-9]", "", (name or "").lower())[:14]
    if not want:
        return None
    for v in data.values():
        have = re.sub(r"[^a-z0-9]", "", v["title"].lower())[:14]
        if want == have:
            return v["ticker"].upper()
    return None


_SPIN_PARENT_PATTERNS = (
    # Exhibit list: "Form of [Separation and] Distribution Agreement between PARENT and SPINCO"
    r"(?:Separation and )?Distribution Agreement,?\s+(?:dated[^,]*,\s*)?between\s+([A-Z][\w&,\.\s]{2,78}?)\s+and\s+[A-Z]",
    # Item 10 (Recent Sales of Unregistered Securities): "PARENT acquired N shares of ... SPINCO"
    r"([A-Z][\w&,\.\s]{2,78}?)\s+acquired\s+[\d,]+\s+(?:uncertificated\s+)?shares? of (?:common|capital) stock of",
    r"wholly[- ]owned subsidiary of\s+([A-Z][\w&,\.\s]{2,78}?)[\.,]",
)


def _spin_parent_ticker(doc_text):
    """A pre-distribution spinco has no ticker of its own by definition. Every spinoff
    Form 10 discloses the PARENT in near-identical boilerplate — the Distribution
    Agreement exhibit and Item 10's initial-share issuance are both standard disclosure
    items, present whether or not the filer ever says "spin-off" in prose (scout.py-037,
    2026-08-22)."""
    for pat in _SPIN_PARENT_PATTERNS:
        m = re.search(pat, doc_text)
        if m:
            tk = _name_to_ticker(m.group(1).strip())
            if tk:
                return tk
    return None


def _resolve_spin_parents(rows, cap=10):
    """Resolve the PARENT's ticker for each spin-registration row so it can triage
    before the spinco itself ever trades, and keep the spinco's own CIK on the row
    (spinco_cik) so it can graduate to its own ticker once the distribution completes
    instead of being re-discovered as a new lead (scout.py-037)."""
    cache_f = DATA / "spin_parents.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        cache = {}
    fetched = 0
    for r in rows:
        r["spinco_cik"] = r.get("cik")
        if r.get("ticker"):   # already trading under its own symbol — nothing to resolve
            continue
        acc = r["url"].rsplit("/", 1)[-1]
        if acc in cache:
            if cache[acc]:
                r["ticker"] = cache[acc]
            continue
        if fetched >= cap:  # politeness: resolve the backlog across successive runs
            continue
        fetched += 1
        tk = None
        try:
            raw = requests.get(r["url"], headers=UA, timeout=30).content[:150000]
            time.sleep(0.15)
            text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore"))
            text = re.sub(r"&nbsp;|&#\d+;", " ", text)
            text = re.sub(r"\s+", " ", text)
            tk = _spin_parent_ticker(text)
        except Exception:
            pass
        cache[acc] = tk
        if tk:
            r["ticker"] = tk
    cache_f.write_text(json.dumps(cache, indent=1))
    return rows


# build-004 (PM adjudication 2026-08-27): the ask's literal channel -- a market-wide watch
# for 8-Ks carrying items 1.01+3.02+3.03 ("Plan Effective Date" language) -- needs the ITEMS
# field, which only exists per-CIK in submissions.json, not in the daily index; scanning
# every market-wide 8-K to find it is not a cheap first cut. PM's own adjudication (having
# just hand-recovered KODK -- an S-3 resale shelf for 4,426,268 sponsor shares, effective
# 2026-07-08 -- from the Bench's stranded claims) named the cheaper substitute: EDGAR's daily
# index carries a distinct "EFFECT" form for every registration statement's notice of
# effectiveness, ~14/day market-wide. Each notice's own XML names the underlying form (S-3,
# S-4, S-8, N-1A, ...) and file number -- so filtering to the S-3/S-1 family before any
# further fetch turns a noisy 14/day radar into a handful of leads that WOULD have caught
# KODK's channel. Classifying resale-vs-primary-shelf is left to scout.py's existing
# pre-triage stage (same division of labor as every other radar channel: code collects the
# minimal fact, the local model reads the event text for mechanism fit) rather than guessed
# here from more brittle document-text heuristics.
REG_FORM_PREFIXES = ("S-3", "S-1")


def _resolve_reg_effectiveness(rows, cap=20):
    """rows are raw EFFECT-notice hits from the daily index (company/cik/date/url only --
    the index line itself doesn't say what type of registration went effective). Fetch each
    notice's own SGML/XML body once, cached forever by its accession (a notice of
    effectiveness is never amended), to learn the underlying form and keep only the S-3/S-1
    family this channel cares about."""
    cache_f = DATA / "reg_effective.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        cache = {}
    fetched = 0
    kept = []
    for r in rows:
        acc = r["url"].rsplit("/", 1)[-1]
        cached = cache.get(acc)
        if cached is None and fetched < cap:  # politeness: resolve the backlog across successive runs
            fetched += 1
            cached = {}
            try:
                text = requests.get(r["url"], headers=UA, timeout=30).text
                fm = re.search(r"<form>\s*([^<]+?)\s*</form>", text)
                em = re.search(r"<finalEffectivenessDispDate>\s*([^<]+?)\s*</finalEffectivenessDispDate>", text)
                nm = re.search(r"<fileNumber>\s*([^<]+?)\s*</fileNumber>", text)
                if fm:
                    cached = {"reg_form": fm.group(1).strip(),
                              "effective_date": em.group(1).strip() if em else None,
                              "file_number": nm.group(1).strip() if nm else None}
            except Exception:
                pass
            time.sleep(0.15)
            cache[acc] = cached
        if cached and cached.get("reg_form", "").startswith(REG_FORM_PREFIXES):
            r.update(cached)
            kept.append(r)
    cache_f.write_text(json.dumps(cache, indent=1))
    return kept


def special_situations(days=10):
    ticker_by_cik = {int(c): t for t, c in cik_map().items()}
    out = {"sc13d": [], "spins": [], "delistings": [], "reg_effective": [], "n14": []}
    seen = set()
    d = dt.date.today()
    fetched = 0
    while fetched < days:
        if d.weekday() < 5:  # weekdays only
            q = (d.month - 1) // 3 + 1
            url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d.strftime('%Y%m%d')}.idx"
            try:
                r = requests.get(url, headers=UA, timeout=30)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        row = _parse_idx_line(line)
                        if not row:
                            continue
                        key = (row["form"], row["url"].rsplit("/", 1)[-1])
                        if key in seen:
                            continue
                        seen.add(key)
                        try:
                            row["ticker"] = ticker_by_cik.get(int(row["cik"]))
                        except ValueError:
                            row["ticker"] = None
                        out[RADAR_FORMS[row["form"]]].append(row)
            except Exception:
                pass
            fetched += 1
            time.sleep(0.15)
        d -= dt.timedelta(days=1)
    # scout.py-172: the [:60] cap below drops any channel that collected more than 60
    # rows over the window with NO count of what it dropped — a channel running hot
    # (a busy 13D week, a cluster of index deletions) silently lost the tail with
    # nothing to show for it. Record before/after per channel.
    pre_cap = {k: len(v) for k, v in out.items()}
    for k in out:
        out[k] = sorted(out[k], key=lambda x: x["date"], reverse=True)[:60]
    funnel_record("feeds:radar", sum(pre_cap.values()), sum(len(v) for v in out.values()),
                  by_channel={k: {"in": pre_cap[k], "out": len(out[k])} for k in out})
    # The 20/10/20 resolver caps (politeness: one run resolves at most N NEW accessions)
    # mostly leave a row unresolved rather than dropping it (13D/spins keep every row,
    # just without a subject/parent ticker until a later run's cache catches up) — EXCEPT
    # reg_effective, whose resolver also FILTERS to the S-3/S-1 family, so an unresolved
    # row (cap exceeded, nothing cached yet) is invisible this run, not just unlabeled.
    # Record before/after each resolver so a persistently-growing unresolved backlog is
    # visible instead of looking identical to "nothing to resolve."
    n_13d_in = len(out["sc13d"])
    out["sc13d"] = _resolve_13d_subjects(out["sc13d"])
    funnel_record("feeds:resolve_13d", n_13d_in, sum(1 for r in out["sc13d"] if r.get("subject")))
    n_spins_in = len(out["spins"])
    out["spins"] = _resolve_spin_parents(out["spins"])
    funnel_record("feeds:resolve_spins", n_spins_in, sum(1 for r in out["spins"] if r.get("ticker")))
    n_reg_in = len(out["reg_effective"])
    out["reg_effective"] = _resolve_reg_effectiveness(out["reg_effective"])
    funnel_record("feeds:resolve_reg_effective", n_reg_in, len(out["reg_effective"]))
    return out


def _tag_held(situations, held):
    """A 13D/spin/delisting/reg-effectiveness on a name we hold or watch is not one of
    dozens of market-wide rows, it is a tripwire on our own book — mark it so a reader (or a
    future scorer) does not have to cross-reference by hand (feeds.py-011: a TLS
    Schedule 13D sat unflagged in this exact radar). sc13d uses subject_ticker (the
    daily index's own 'ticker' field is the FILER's, resolved separately); spins,
    delistings and reg_effective key off the issuer's own CIK, so 'ticker' is already
    the subject."""
    held_hits = []
    for r in situations.get("sc13d") or []:
        tk = r.get("subject_ticker")
        r["held"] = bool(tk and tk in held)
        if r["held"]:
            held_hits.append({"kind": "sc13d", "ticker": tk, "date": r.get("date"), "url": r.get("url")})
    for key in ("spins", "delistings", "reg_effective", "n14"):
        for r in situations.get(key) or []:
            tk = r.get("ticker")
            r["held"] = bool(tk and tk in held)
            if r["held"]:
                held_hits.append({"kind": key, "ticker": tk, "date": r.get("date"), "url": r.get("url")})
    situations["held_hits"] = sorted(held_hits, key=lambda x: x.get("date") or "", reverse=True)
    return situations


# ---------- manager tracker (13F holdings diff) ----------
# What professional coattailing actually is: quarterly 13F-HR info tables for a
# curated manager list (managers.txt — agent-owned, CIKs verified 2026-08-12),
# diffed vs the prior quarter. New stakes / big adds / exits are IDEA FLOW with
# a 45-day lag, never a thesis (SOURCING.md bar unchanged). Cached 24h — 13Fs
# are quarterly; refetching every 30 min would be noise.
def _managers():
    out = []
    f = HERE / "managers.txt"
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "|" in line:
            cik, name, *style = line.strip().split("|")
            out.append({"cik": cik.strip(), "name": name.strip(),
                        "style": style[0].strip() if style else ""})
    return out


def _13f_holdings(cik, acc):
    """Parse one 13F-HR info table -> {cusip+putCall: {issuer, value, shares}}."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    try:
        items = requests.get(f"{base}/index.json", headers=UA, timeout=30).json()["directory"]["item"]
    except Exception:
        return None
    xmls = [i["name"] for i in items if i["name"].lower().endswith(".xml")
            and "primary_doc" not in i["name"].lower()]
    if not xmls:
        return None
    try:
        raw = requests.get(f"{base}/{xmls[0]}", headers=UA, timeout=60).text
    except Exception:
        return None
    time.sleep(0.15)
    hold = {}
    for m in re.finditer(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", raw, re.S):
        b = m.group(1)

        def g(tag):
            mm = re.search(rf"<(?:\w+:)?{tag}>\s*([^<]+?)\s*<", b)
            return mm.group(1) if mm else ""
        cusip, pc = g("cusip"), g("putCall")
        key = cusip + (":" + pc if pc else "")
        try:
            val, sh = float(g("value") or 0), float(g("sshPrnamt") or 0)
        except ValueError:
            continue
        if key in hold:
            hold[key]["value"] += val
            hold[key]["shares"] += sh
        else:
            hold[key] = {"issuer": g("nameOfIssuer"), "cusip": cusip, "putCall": pc,
                         "value": val, "shares": sh}
    return hold


def manager_moves(max_age_h=24):
    cache = DATA / "managers.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_h * 3600:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    out = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "note": "13F info-table diff, latest vs prior quarter; value-weighted; 45-day lag",
           "managers": []}
    for mgr in _managers():
        cik = mgr["cik"].zfill(10)
        try:
            rec = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                               headers=UA, timeout=30).json()["filings"]["recent"]
        except Exception:
            continue
        time.sleep(0.15)
        f13 = [(d, a, rd) for f, d, a, rd in zip(rec["form"], rec["filingDate"],
                                                 rec["accessionNumber"], rec["reportDate"])
               if f == "13F-HR"][:2]
        if not f13:
            continue
        latest = _13f_holdings(mgr["cik"], f13[0][1])
        prior = _13f_holdings(mgr["cik"], f13[1][1]) if len(f13) > 1 else {}
        if not latest:
            continue
        tot = sum(h["value"] for h in latest.values()) or 1

        def row(h, extra=""):
            r = {"issuer": h["issuer"], "cusip": h["cusip"],
                 "pct_port": round(h["value"] / tot * 100, 2)}
            if h.get("putCall"):
                r["putCall"] = h["putCall"]
            if extra:
                r["change"] = extra
            return r
        news, adds, trims, exits = [], [], [], []
        for k, h in latest.items():
            p = (prior or {}).get(k)
            if not p:
                news.append(row(h, "new"))
            elif p["shares"] and h["shares"] >= p["shares"] * 1.3:
                adds.append(row(h, f"+{(h['shares'] / p['shares'] - 1) * 100:.0f}% shares"))
            elif p["shares"] and h["shares"] <= p["shares"] * 0.7:
                trims.append(row(h, f"{(h['shares'] / p['shares'] - 1) * 100:.0f}% shares"))
        for k, p in (prior or {}).items():
            if k not in latest:
                exits.append({"issuer": p["issuer"], "cusip": p["cusip"]})
        top = sorted(latest.values(), key=lambda h: -h["value"])[:10]
        out["managers"].append({
            "name": mgr["name"], "style": mgr["style"], "cik": mgr["cik"],
            "filed": f13[0][0], "period": f13[0][2], "n_positions": len(latest),
            "new": sorted(news, key=lambda r: -r["pct_port"])[:15],
            "adds": sorted(adds, key=lambda r: -r["pct_port"])[:10],
            "trims": sorted(trims, key=lambda r: -r["pct_port"])[:10],
            "exits": exits[:10], "top": [row(h) for h in top]})
    cache.write_text(json.dumps(out, indent=1))
    return out


def _cross_check_earnings(earnings, filings, today_s):
    """Vendor calendars go stale (ARI showed 'reports 2026-08-13' two days AFTER it
    printed on 2026-08-11). EDGAR is truth: an 8-K with Item 2.02 (results of
    operations) in the last 7 days means the company already reported — flag the
    calendar row instead of letting the agent plan around a phantom future print."""
    for e in earnings:
        tk = e.get("symbol", "")
        recent_202 = [f for f in (filings.get(tk) or [])
                      if f.get("form") == "8-K" and "2.02" in (f.get("items") or "")
                      and f.get("date", "") >= (dt.date.today() - dt.timedelta(days=7)).isoformat()]
        if e.get("date", "") <= today_s:
            e["in_past"] = True
        if recent_202:
            e["already_reported"] = True
            e["evidence"] = f"8-K item 2.02 filed {recent_202[0]['date']} (EDGAR beats this calendar)"
    return earnings


def refresh():
    DATA.mkdir(exist_ok=True)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    today_s = dt.date.today().isoformat()
    tks = universe()
    news = company_news(tks)
    filings = edgar_filings(tks)
    feed = {
        "fetched_at": now_iso,
        "_doc": "All timestamps ISO-8601 UTC with year. Sections carry their own as_of; "
                "distrust any section older than fetched_at implies. earnings rows may carry "
                "already_reported/in_past flags (EDGAR 8-K 2.02 cross-check beats the vendor calendar).",
        "universe": tks,
        "news": news,
        "market_news": market_news(),
        "earnings": _cross_check_earnings(earnings_calendar(tks), filings, today_s),
        "filings": filings,
        "situations": _tag_held(special_situations(), set(tks)),
        "managers": manager_moves(),
        "as_of": {"news": now_iso, "market_news": now_iso, "earnings": now_iso,
                  "filings": now_iso, "situations": now_iso},
    }
    try:  # managers section refreshes on its own 24h cadence — surface its real age
        feed["as_of"]["managers"] = feed["managers"].get("fetched_at")
    except Exception:
        pass

    # A failed vendor call returns nothing, and nothing is indistinguishable from "no
    # news in the world" once it has been written down with a fresh timestamp. That is
    # exactly what happened at 2026-08-13T21:30Z: finnhub's general-news call failed,
    # market_news went to [], and as_of.market_news still said "just now" — so the feed
    # asserted a quiet market instead of admitting a missing call. Carry the last good
    # section forward and KEEP ITS ORIGINAL as_of, so it reads stale (true) rather than
    # empty-and-current (false). earnings is excluded: it is legitimately empty when
    # nothing is due inside the window.
    prev = {}
    try:
        prev = json.loads((DATA / "feed.json").read_text())
    except Exception:
        pass

    def _empty(v):
        return not v or (isinstance(v, dict) and not any(v.values()))

    degraded = []
    for sec in ("news", "market_news", "filings", "situations"):
        if _empty(feed.get(sec)) and not _empty(prev.get(sec)):
            feed[sec] = prev[sec]
            feed["as_of"][sec] = (prev.get("as_of") or {}).get(sec) or prev.get("fetched_at")
            degraded.append(sec)
    feed["degraded"] = degraded
    feed["vendor_errors"] = FH_FAILS[-8:]
    if degraded:
        feed["_doc"] += (" DEGRADED: " + ", ".join(degraded) + " could not be refreshed this run and are "
                         "carried over from the previous good fetch — read their as_of, not fetched_at.")

    _write_json(DATA / "feed.json", feed)
    n_news = sum(len(v) for v in feed["news"].values())
    n_fil = sum(len(v) for v in feed["filings"].values())
    sit = feed["situations"]
    n_held_hits = len(sit.get("held_hits") or [])
    print(f"feed.json: {len(tks)} tickers · {n_news} news · {n_fil} filings · "
          f"{len(feed['earnings'])} earnings · {len(feed['market_news'])} market headlines · "
          f"radar: {len(sit['sc13d'])} 13Ds, {len(sit['spins'])} spins, {len(sit['delistings'])} delistings, "
          f"{len(sit['reg_effective'])} reg-effective (S-3/S-1), {len(sit['n14'])} N-14 fund reorgs"
          + (f" ({n_held_hits} on held/universe names)" if n_held_hits else "")
          + (f"  ⚠ DEGRADED (carried over): {', '.join(degraded)}" if degraded else "")
          + (f"  [vendor: {'; '.join(FH_FAILS[-3:])}]" if FH_FAILS else ""))

    resolved = resolve_dated_form_expectations()
    if resolved:
        print(f"dates.json: {len(resolved)} dated form expectation(s) resolved — " + "; ".join(resolved))


if __name__ == "__main__":
    if sys.argv[1:2] == ["refresh"]:
        refresh()
    elif sys.argv[1:2] == ["dates"]:
        for line in resolve_dated_form_expectations():
            print(line)
    else:
        sys.exit("usage: feeds.py refresh | feeds.py dates")
