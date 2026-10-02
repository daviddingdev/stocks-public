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
  Stage 2  ENRICH (code):   plausibility >=4 (or >=3 with a resolved ticker) -> fincard
                            built (codified numbers). Loosened from >=5 after the ETD
                            case (2026-08-14): the one lead the PM promoted by reading
                            the 13D itself, going around a funnel that had scored it 4
                            and buried it — see the gate comment in run() for the full
                            record; this docstring used to still say >=5 (scout.py-172).
  Stage 3  TRIAGE (local, event + numbers): mechanism-fit 0-10 against the
                            doctrine's channels + a 4-sentence sketch + what the
                            variant perception would have to be + red flags.
  Stage 4  PM (Opus):       reads the ranked queue top in its daily session;
                            every pass/decline recorded on the candidate.

data/candidates.json lifecycle: new -> pre_triaged -> triaged -> pm_reviewed ->
underwriting | passed | dropped.  Zero Claude tokens anywhere in this file.
CLI: scout.py run | scout.py list | scout.py covers [N]   (stage 0b backfill, no model)
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
sys.path.insert(0, str(HERE))
from feeds import funnel_record  # noqa: E402  — scout.py-172 funnel counts

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
"channel": "<one channel name or none>",
"trigger": "<the words in the EVENT's detail line that drove your score, copied verbatim, max 15 words — empty if nothing in the line supports a score above 2>"}}"""

TRIAGE_PROMPT = f"""You are the junior sourcing analyst for a mechanism-driven value fund.
Channels: {CHANNELS}.
Given the EVENT and the code-computed FINANCIAL CARD SUMMARY, score MECHANISM FIT 0-10
(not cheapness — a stated non-value seller + a dated path matters more than a low multiple).
Answer the doctrine's questions. NUMBERS RULE: any percentage or dollar figure you write in
the sketch must be COPIED from the EVENT's own detail line, not computed, rounded, or
imported from the FINANCIAL CARD SUMMARY — the card is background context for your judgment,
not a source to quote from. Never call a figure "y/y" unless its own line says so; a figure
tagged with a span (e.g. "over 1365 days") is that span, not a year. JSON:
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
                "company", "holdings", "holding", "hldgs", "hldg", "group", "the", "plc"}


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


def _local_read_seller(tk, kind):
    """scout.py-215: the registered share count/pct lives in the S-1/S-3's own
    selling-stockholder table, not in feed.json's EFFECT-notice row — local_read.py
    (vp.py-173's LOCAL READ stage, shipped seller-only 2026-09-18) reads that table and
    verifies the quote verbatim. Returns (holder, shares_or_pct) or (None, None) when no
    verified seller read exists yet for this ticker/kind."""
    if not tk:
        return None, None
    d = _j(NAMES / tk / "local_read.json", {})
    for e in reversed(d.get("seller") or []):
        if e.get("kind") == kind and e.get("verified"):
            return e.get("holder") or None, e.get("shares_or_pct") or None
    return None, None


IDX_EVENT_D = 60


def _index_deletion_detail(r):
    d = (f"{r.get('index')} DELETION of {r.get('company')} effective before the open {r.get('effective')} — "
         f"S&P DJI press release {r.get('release_date')} ({r.get('release_url')})")
    if r.get("why"):
        d += f': "{r["why"][:300]}"'
    if r.get("print_close") is not None and r.get("now_close") is not None:
        d += (f" · forced-sale print: close ${r['print_close']:,.2f} on {r['print_date']}, volume "
              f"{r['print_volume']:,} = {r.get('vol_mult_8d')}x the prior 8 sessions' mean · now "
              f"${r['now_close']:,.2f} ({r['now_date']} close) = {r['vs_print_pct']:+.2f}% vs the print -> "
              + ("CONSUMED (R-37: > +2% above the print)" if r.get("consumed")
                 else "NOT consumed (R-37: within +2% of the print or below it)"))
    elif r.get("print_pending"):
        d += f" · {r['print_pending']}"
    elif r.get("price_error"):
        d += f" · price not computed: {r['price_error']}"
    card = _j(NAMES / str(r.get("ticker")) / "fincard.json", {})
    D = card.get("derived") or {}
    bits = [f"{lab} {fmt(D[k]['value'])}" for k, lab, fmt in (
        ("market_cap", "cap", lambda v: f"${v / 1e6:,.0f}M"),
        ("net_cash", "net cash", lambda v: f"${v / 1e6:,.0f}M"),
        ("fcf_yield_pct", "FCF yield", lambda v: f"{v:.1f}%"))
        if isinstance((D.get(k) or {}).get("value"), (int, float))]
    if bits:
        d += f" · card ({str(card.get('built', ''))[:10]}): " + ", ".join(bits)
    return d


def _events():
    """Stage 0: normalized events with stable ids from the feeds."""
    feed = _j(DATA / "feed.json", {})
    ev = []
    sit = feed.get("situations") or {}
    for r in (sit.get("sc13d") or []):
        # r["company"] is the daily-index label, which is unreliable for filer identity —
        # EDGAR indexes a 13D under both parties' CIKs, and the surviving row after dedup
        # is arbitrary (feeds.py-049). r["filer"], resolved from the submission header's own
        # FILED BY block, is authoritative; fall back to the index label only until resolved.
        filer = r.get("filer") or r.get("company")
        if _self_filed_13d(filer, r.get("subject")):
            continue
        if r.get("frac_shares_basis"):
            # scout.py-061: a fractional (to 2+ decimals) share-count basis in the 13D's own
            # Item 5 percentage math is an interval/tender-offer fund's signature — it
            # repurchases at NAV, not a listed price, so there is no discount for an
            # activist to close. Applies to the 13D channel generally, not just a CEF screen.
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
                   "detail": f"SCHEDULE 13D by {filer or '?'} on "
                             f"{r.get('subject', tk or '?')}", "url": r.get("url")})
    for r in (sit.get("spins") or []):
        # ticker (if resolved) is the PARENT's — a pre-distribution spinco has none of
        # its own; spinco_cik is kept so the row can graduate once the spinco itself
        # starts trading, instead of being re-discovered as a new lead (scout.py-037).
        ev.append({"id": f"spin:{r.get('cik')}:{r.get('date')}", "kind": "spin-registration",
                   "ticker": r.get("ticker"), "spinco_cik": r.get("spinco_cik") or r.get("cik"),
                   "date": r.get("date"),
                   "detail": f"Form 10-12B: {r.get('company', '?')}", "url": r.get("url")})
    for r in (sit.get("reg_effective") or []):
        # build-004 (PM 2026-08-27, KODK's sponsor-share resale shelf as the worked example):
        # a market-wide watch for S-3/S-1 registration statements going EFFECTIVE. This does
        # NOT yet distinguish a resale shelf (creditor/sponsor shares registered for public
        # sale -- the post-reorg mechanism the ask actually wants) from a primary capital
        # raise -- that read belongs to the SAME pre-triage/triage stages every other channel
        # here uses, not to a document-text heuristic guessed at collection time. id keyed on
        # cik+file_number (both stable regardless of ticker-resolution status) -- same lesson
        # as the 13D id bug two channels up.
        holder, shares_or_pct = _local_read_seller(r.get("ticker"), "reg_effective")
        detail = (f"{r.get('reg_form', '?')} registration effective "
                  f"{r.get('effective_date', '?')} for {r.get('company', '?')} "
                  f"(file {r.get('file_number', '?')}) — resale/selling-stockholder "
                  f"shelf or primary raise?")
        if holder:
            # vp.py-173 LOCAL READ verified this against the S-1/S-3's own text — a resale
            # shelf, not a guess, and the number the pre-triage prompt can now cite instead
            # of asking "shelf or primary raise?" with nothing to answer it.
            detail += f" RESALE SHELF confirmed: {holder}" + (f", {shares_or_pct}" if shares_or_pct else "")
        ev.append({"id": f"regfx:{r.get('cik')}:{r.get('file_number')}", "kind": "reg-effective",
                   "ticker": r.get("ticker"), "date": r.get("effective_date") or r.get("date"),
                   "detail": detail, "url": r.get("url")})
    for r in (sit.get("n14") or []):
        # build-003: N-14 registers a fund merger/reorganization -- including a mutual-
        # fund/CEF converting into an ETF share class, the specific pattern the ask names.
        # Whether THIS N-14 is a CEF-into-ETF conversion (vs. a routine two-fund merger
        # inside the same family) is a triage read, same division of labor as reg-effective.
        ev.append({"id": f"n14:{r.get('cik')}:{r.get('date')}", "kind": "n14-reorg",
                   "ticker": r.get("ticker"), "date": r.get("date"),
                   "detail": f"N-14 fund reorganization/merger registration: {r.get('company', '?')} "
                             f"— check for CEF/mutual-fund-into-ETF conversion language",
                   "url": r.get("url")})
    # scout.py-261 (PM 2026-09-23): index DELETIONS leaving the S&P Composite 1500, read and
    # priced by feeds.index_deletions() from S&P DJI's own releases. Acquired names (the
    # release says so, or the tape stopped after the print) are a deal price, not a forced
    # seller, and are not minted; a deletion more than IDX_EVENT_D past its effective date is
    # history. The detail carries the whole R-37 test so pre-triage and the PM read the same
    # numbers the PM computed by hand on 2026-09-23.
    idx_from = (dt.date.today() - dt.timedelta(days=IDX_EVENT_D)).isoformat()
    for r in (sit.get("index_deletions") or []):
        tk, eff = r.get("ticker"), r.get("effective")
        if not tk or not eff or eff < idx_from or r.get("acquired") or r.get("delisted"):
            continue
        ev.append({"id": f"idxdel:{tk}:{eff}", "kind": "index-deletion", "ticker": tk, "date": eff,
                   "detail": _index_deletion_detail(r), "url": r.get("release_url")})
    # scout.py-261 note 2: COMPLETED spins (distribution in the last 90 days), from the same
    # S&P DJI releases — the spinco's first-week low is the flush print, today's price vs it
    # is how much of the flush is left
    for r in (sit.get("spins_completed") or []):
        tk = r.get("ticker")
        if not tk or not r.get("completed"):
            continue
        d = (f"COMPLETED SPIN: {r.get('parent')} spun off {r.get('company')} ({tk}), completion "
             f"{r['completed']} per S&P DJI press release {r.get('release_date')} ({r.get('release_url')}): "
             f"\"{(r.get('quote') or '')[:260]}\"")
        if r.get("first_week_low") is not None:
            d += (f" · first-week low ${r['first_week_low']:,.2f} ({r['first_week_low_date']}, min daily low of "
                  f"the first {r.get('first_week_sessions')} sessions from {r['completed']}) · now "
                  f"${r['now_close']:,.2f} ({r['now_date']} close) = {r['vs_low_pct']:+.2f}% vs that low")
        elif r.get("price_error"):
            d += f" · price not computed: {r['price_error']}"
        ev.append({"id": f"spindone:{tk}:{r['completed']}", "kind": "spin-completed", "ticker": tk,
                   "date": r["completed"], "detail": d, "url": r.get("release_url")})
    # scout.py-172 (funnel audit 2026-09-07): STRATEGY-PROPOSAL-v3 §1 "Deleted as a
    # source": 13F flow (a 13F names a buyer, never a seller) and insider clusters as a
    # mechanism ON THEIR OWN (a cluster is a CONFIRMER on a name that already has a
    # seller from another channel, never the seller itself). Both used to mint their own
    # scout.py candidate rows with no marker distinguishing them from an admissible
    # channel, spending pre-triage calls and VP desk slots on a source the strategy has
    # explicitly ruled out. Stopped minting rather than stamping admissible:false (the
    # ask's other option) — there's no other reader of that field yet, and a live
    # candidate.json row that can never leave "new" is the exact kind of dead weight
    # the ORIGINAL scout.py memory-of-rejection design (see the module docstring) exists
    # to avoid creating in the first place. Existing rows of these kinds (any PM verdict
    # already on file) are untouched — only new minting stops.
    can = _j(DATA / "cannibal.json", {})
    # scout.py-172 item 3: 'top' (15) was the only bucket ingested; cannibal.py's own
    # all_hits (21, superset of top) and extreme_shrink (screened OUT of the ranked list
    # for an implausibly large shrink %, but still a real hit worth a human look) never
    # became candidates at all. Union all three, ticker-deduped by bucket priority
    # (top > all_hits > extreme_shrink — top is already the algorithmically-ranked
    # subset), and carry which bucket produced the row so triage/the PM can see it wasn't
    # a top-15 name.
    can_ran_at = None
    try:
        can_ran_at = dt.datetime.fromisoformat(str(can.get("ran_at", "")).replace("Z", "+00:00"))
    except Exception:
        pass
    seen_tk = set()
    for bucket in ("top", "all_hits", "extreme_shrink"):
        for h in can.get(bucket, []):
            tk = h.get("ticker")
            if not tk or tk in seen_tk:
                continue
            seen_tk.add(tk)
            # cannibal.py-227 (David 2026-09-16, C17 on can:KFY): cannibal.json is only as
            # fresh as its own run cadence (weekly), but this loop re-quotes it into the
            # candidate detail EVERY scout.py run — so a row can keep asserting a frames-
            # derived net cash for days after our own fincard (built later, tag-precedence +
            # zero-proof) corrects it. If a fincard exists and postdates the screen, re-quote
            # net cash/cap/FCF yield from the card rather than the stale screen figures.
            nc, mc, fy, src_note = h.get("net_cash"), h.get("market_cap"), h.get("fcf_yield_pct"), ""
            card_path = HERE / "names" / tk / "fincard.json"
            if can_ran_at is not None and card_path.exists():
                try:
                    card = json.loads(card_path.read_text())
                    built = dt.datetime.fromisoformat(str(card.get("built", "")).replace("Z", "+00:00"))
                    if built > can_ran_at:
                        d = card.get("derived") or {}
                        cnc = (d.get("net_cash") or {}).get("value")
                        cmc = (d.get("market_cap") or {}).get("value")
                        cfy = (d.get("fcf_yield_pct") or {}).get("value")
                        if cnc is not None:
                            nc = cnc
                        if cmc is not None:
                            mc = cmc
                        if cfy is not None:
                            fy = cfy
                        if cnc is not None or cmc is not None or cfy is not None:
                            src_note = " (re-quoted from fincard, newer than screen)"
                except Exception:
                    pass
            # Keyed by TICKER, not ticker+date (scout.py-056): the screen re-hits the same
            # names every run it survives on, and a dated id minted a fresh row per sighting —
            # 120 rows for 25 tickers, with a PM verdict (dropped/pm_reviewed) on one day's row
            # invisible to tomorrow's. The merge loop below carries date/detail/runs_on_screen
            # forward on the SAME row and never touches status/pm_note, so a verdict sticks
            # until a human reopens it — "never re-derive a candidate you already passed on
            # without new facts" (SOURCING.md) is now something the funnel can actually do.
            ev.append({"id": f"can:{tk}",
                       "kind": "cannibal-screen", "ticker": tk, "date": str(can.get("ran_at", ""))[:10],
                       "bucket": bucket,
                       "runs_on_screen": h.get("runs_on_screen") or h.get("weeks_on_screen") or 1,
                       "detail": (f"Cannibal screen [{bucket}]: FCF yield {fy}%, shares "
                                  f"-{h['share_shrink_pct']}% y/y, net cash ${nc / 1e6:,.0f}M, "
                                  f"cap ${mc / 1e6:,.0f}M, {h.get('runs_on_screen') or h.get('weeks_on_screen') or 1} run(s) on screen"
                                  + src_note
                                  + (f" · {h['fcf_note']}" if h.get("fcf_note") else "")
                                  + " — a screen hit is a QUESTION: why is it cheap, who is the wrong-price seller?")})
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
    """13F rows carry issuer NAMES; best-effort exact-ish match against the SEC map.
    Falls back to a token-set match (stripping legal-form words, same set _self_filed_13d
    uses) when the 14-char prefix compare misses on an abbreviated legal form a 13F filer
    used but the SEC's own title didn't — "LAMB WESTON HLDGS INC" vs the registered
    "Lamb Weston Holdings, Inc." differ at char 12 ('hldg' vs 'hold'), which stranded a
    Starboard Value 5.65%-of-book stake at 'no_ticker' for 9 days (scout.py-037)."""
    if not issuer:
        return None
    data = _sec_map()
    if not data:
        return None
    want = re.sub(r"[^a-z0-9]", "", issuer.lower())[:14]
    want_toks = _name_tokens(issuer)
    for v in data.values():
        have = re.sub(r"[^a-z0-9]", "", v["title"].lower())[:14]
        if want and want == have:
            return v["ticker"].upper()
    if want_toks:
        for v in data.values():
            if _name_tokens(v["title"]) == want_toks:
                return v["ticker"].upper()
    return None


_SPAN_DAYS_RE = re.compile(r"over (\d+) days")


_PCT_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*(?=\s?%)")
_USD_NUM_RE = re.compile(r"\$\s?([\d,]*\.?\d*)\s?(million|billion|thousand|[MBK])?\b", re.I)
_USD_MULT = {"million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9, "thousand": 1e3, "k": 1e3}


def _pct_nums(text):
    return [float(m.replace(",", "")) for m in _PCT_NUM_RE.findall(text or "")]


def _usd_nums(text):
    out = []
    for num, unit in _USD_NUM_RE.findall(text or ""):
        if not num:
            continue
        out.append(float(num.replace(",", "")) * _USD_MULT.get(unit.lower(), 1))
    return out


def _unsupported_figures(sketch, detail):
    """scout.py-029: the triage model quoted a share-count change from the FINANCIAL CARD
    SUMMARY into the sketch and mislabeled its multi-year span "y/y" — a number nobody
    computed for the claim it was attached to reaching the PM's desk as if it had. A % or $
    figure in the sketch that is not (within rounding) also in the row's own coded `detail`
    line did not come from a vetted source and is flagged rather than trusted silently."""
    det_pct, det_usd = _pct_nums(detail), _usd_nums(detail)
    bad = [f"{v:g}%" for v in _pct_nums(sketch)
           if not any(abs(v - d) <= max(0.5, abs(d) * 0.05) for d in det_pct)]
    bad += [f"${v:,.0f}" for v in _usd_nums(sketch)
            if not any(abs(v - d) <= max(v, d, 1) * 0.05 for d in det_usd)]
    return bad


# ---------------------------------------------------------------- reg-effective: cover + gate
# scout.py-267 (PM 2026-09-25): an EFFECT notice says a registration went effective and
# nothing else, so every reg-effective row reached pre-triage as "shelf or primary raise?"
# and was scored "Routine S-1/S-3 filing" — 1 of 120 rows ever carried a named seller,
# while the registration statements' own covers showed 30 of 76 September rows were
# resale registrations (KRP: 9,500,000 units = 9.42%; PED: 11,040,909 sh = 83.06%, both
# found by hand). Code now opens the document filed under the EFFECT's file number at
# collection, quotes its cover sentence verbatim, tags RESALE / PRIMARY / MIXED /
# UNCLEAR, and computes N as a % of the outstanding count printed in the SAME document.
# No model anywhere in this stage; the cover read is stored on the row, so no row is
# fetched twice and there is no side file to declare.
_REG_SKIP_FORMS = {"EFFECT", "CORRESP", "UPLOAD", "RW", "AW", "DEL AM", "RW WD", "AW WD",
                   "10-Q", "10-K", "8-K", "S-8", "S-8 POS"}
# a sentence runs to the first period NOT followed by a digit — "par value $0.0001" is not
# its end (the PM's probe regex stopped there and lost the share count on half the rows)
_SENT = r"(?:[^.]|\.(?=\d))*\."
_COVER_RES = [
    re.compile(r"This prospectus relates to" + _SENT),
    re.compile(r"This prospectus (?:covers|registers)" + _SENT),
    re.compile(r"(?:The|a|certain) selling (?:stock|share|unit|security|securities)holders?[^.]{0,200}? "
               r"(?:may|are) (?:offer|offering|sell|resell)" + _SENT, re.I),
    re.compile(r"(?:We|The Company) (?:may (?:offer and sell|offer|sell|issue)|will (?:offer and sell|offer|sell))"
               + _SENT),
    # a combined shelf's primary half, mid-sentence (MBUU: "From time to time, in one or more
    # offerings, we may offer up to $300,000,000 ...")
    re.compile(r"in one or more offerings, we may (?:offer|sell)" + _SENT),
    re.compile(r"We are offering" + _SENT),
]
# the red-herring legend names the selling holder without saying anything ("Neither we nor
# the Selling Stockholder may sell these securities until the registration statement ... is
# effective") — J.Jill's first backfill quote was that legend, not its cover
_LEGEND_RE = re.compile(r"until the registration statement|Neither we nor|have not authorized|"
                        r"not an offer to sell", re.I)
_RESALE_RE = re.compile(r"\bresale\b|\bresell|selling (?:stock|share|unit|security|securities)holder"
                        r"|by the holders? of|by (?:the )?selling", re.I)
_PRIMARY_RE = re.compile(r"\b(?:we|the company) (?:may|will|are) (?:offer|sell|issu)|\bwe are offering\b"
                         r"|offered by us\b|issuance by us|by us of", re.I)
_ISSUABLE_RE = re.compile(r"issuable upon|upon (?:the )?(?:exercise|conversion)|warrant|convertible|"
                          r"pre-funded|equity line|purchase agreement", re.I)
_N_RE = re.compile(r"(?:up to|aggregate of|of)\s+(?:an aggregate of\s+)?([\d,]{5,})\s+(?:shares|common units|"
                   r"units|ordinary shares|American Depositary Shares|ADSs|subordinate voting shares|"
                   r"Class [A-Z] (?:common|ordinary))", re.I)
# the outstanding count, most specific phrasing first: the Offering summary's "outstanding
# prior to this offering", then "based on N shares outstanding as of", then the looser
# "N shares ... outstanding" — whose 80 chars of left context must not be about warrants/
# options (MPLT's first bare match was its pre-funded-warrant count)
# a share count, never a fragment of one: ",895,984" inside "100,895,984" is not a number
# (the first backfill read KRP's denominator that way and printed 1,060% for a 9.42% shelf)
_NUM = r"(?<![\d,.])(\d{1,3}(?:,\d{3})+|\d{5,})(?![\d,]*\d)"
_OUT_RES = [
    re.compile(r"outstanding (?:prior to|before) (?:this|the) offering:?\s*(?:\(\d\))?\s*" + _NUM, re.I),
    re.compile(r"based on (?:approximately )?" + _NUM + r" (?:shares|common units|ordinary shares)"
               r"[^.\d]{0,80}?outstanding as of", re.I),
    re.compile(r"(?:common stock|common units|ordinary shares)[^.]{0,40}? outstanding"
               r"(?: as of [A-Z][a-z]+ \d{1,2}, \d{4})?:?\s*" + _NUM, re.I),
    re.compile(_NUM + r" (?:shares|common units|ordinary shares)[^.\d]{0,60}? "
               r"(?:were |are )?(?:issued and )?outstanding", re.I),
]


def _sec_get(url, rng=None):
    import urllib.request
    time.sleep(0.15)   # SEC fair-access: well under 10 req/s
    h = dict(UA)
    if rng:
        h["Range"] = f"bytes=0-{rng}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60).read()


def _doc_text(raw):
    import html as _html
    t = raw.decode("utf-8", "ignore")
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).replace("“", '"').replace("”", '"').replace("’", "'")


def _reg_cover(cik, fno):
    """Read the registration statement (or its final 424B prospectus) filed under the
    EFFECT's file number. Returns {tag, form, filed, url, cover, shares, issuable,
    outstanding, outstanding_quote, pct}; tag is RESALE / PRIMARY / MIXED / UNCLEAR, or
    UNREAD when no document sits under the file number. Only the first 600KB is fetched —
    the cover and the Offering summary are always at the front, and the slow link makes a
    full S-1 exhibit bundle a real cost."""
    f = json.loads(_sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"))["filings"]["recent"]
    idx = [i for i in range(len(f["form"]))
           if f["fileNumber"][i] == fno and f["form"][i] not in _REG_SKIP_FORMS]
    if not idx:
        return {"tag": "UNREAD", "why": f"no registration document under {fno} in the recent index"}
    i = idx[0]   # newest first: the final 424B prospectus if one exists, else the last amendment
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{f['accessionNumber'][i].replace('-', '')}/{f['primaryDocument'][i]}")
    t = _doc_text(_sec_get(url, rng=600000))[:120000]
    # every cover-shaped sentence in the front of the document, in document order; the
    # resale sentence and the primary sentence are found separately so a shelf that does
    # both (MBUU: $300M primary + a resale; JILL: 5,000,000 primary + 7,338,933 resale by
    # the Selling Stockholder) tags MIXED instead of whichever sentence came first
    found = []
    for rx in _COVER_RES:
        for m in rx.finditer(t[:40000]):
            if not _LEGEND_RE.search(m.group(0)):
                # "This prospectus relates to" sorts ahead of every other phrasing when no
                # sentence states a count (IPW: its cover sentence, not a later generic one)
                found.append((0 if rx is _COVER_RES[0] else 1, m.start(), m.group(0)[:700]))
                if _N_RE.search(m.group(0)) or rx is not _COVER_RES[2]:
                    break   # a selling-holder sentence without a count: keep looking for one that has it
    if not any(k == 0 for k, _, _ in found):
        # a long table of contents can push "This prospectus relates to" past 40k (IPW)
        m = next((m for m in _COVER_RES[0].finditer(t) if not _LEGEND_RE.search(m.group(0))), None)
        if m:
            found.append((0, m.start(), m.group(0)[:700]))
    if not found:   # nothing cover-shaped in the front at all: first hit anywhere
        for rx in _COVER_RES:
            m = next((m for m in rx.finditer(t) if not _LEGEND_RE.search(m.group(0))), None)
            if m:
                found.append((0, m.start(), m.group(0)[:700]))
                break
    found = [(p, x) for _, p, x in sorted(found)]
    # the resale sentence that STATES the share count wins over an earlier generic one ("the
    # selling stockholders may sell ... in a number of different ways" — XAIR/NXXT/BIAFW)
    res_all = [x for _, x in found if _RESALE_RE.search(x)]
    res_s = next((x for x in res_all if _N_RE.search(x)), res_all[0] if res_all else None)
    pri_s = next((x for _, x in found if _PRIMARY_RE.search(x) and not _RESALE_RE.search(x)), None)
    sent = res_s or pri_s or (found[0][1] if found else None)
    if found:
        res, pri = bool(res_s), bool(pri_s) or bool(res_s and _PRIMARY_RE.search(res_s))
        if pri and res and pri_s and pri_s != res_s:
            sent = f"{res_s} [...] {pri_s}"[:900]
    else:
        # no cover sentence: fall back to the cover region's language
        head = t[:15000]
        res = bool(re.search(r"selling (?:stock|share|unit|security)holders?", head, re.I))
        pri = bool(re.search(r"\bwe (?:may offer|are offering)\b", head, re.I))
    tag = "MIXED" if res and pri else "RESALE" if res else "PRIMARY" if pri else "UNCLEAR"
    n = None
    if res_s if found else (sent and res):
        m = _N_RE.search(res_s if found else sent)
        if m:
            n = float(m.group(1).replace(",", ""))
    out = out_q = None
    for rx in _OUT_RES:
        for m in rx.finditer(t):
            left = t[max(0, m.start() - 80):m.start()]
            if rx is _OUT_RES[-1] and re.search(r"issuable|issuance|reserved|exercise|warrant|option|vesting",
                                                left + m.group(0), re.I):
                continue
            v = float(m.group(1).replace(",", ""))
            if v > 1000 and v != n:
                out, out_q = v, m.group(0)[:200]
                break
        if out:
            break
    return {"tag": tag, "form": f["form"][i], "filed": f["filingDate"][i], "url": url,
            "cover": sent, "shares": n,
            "issuable": bool(res and _ISSUABLE_RE.search((res_s if found else sent) or "")),
            "outstanding": out, "outstanding_quote": out_q,
            "pct": round(100 * n / out, 2) if n and out else None}


def _cover_text(c, card=None):
    """The detail-line suffix for a read cover: verbatim sentence, tag, N and N/outstanding
    with the denominator's source named. A fincard share count is used only when the
    document prints none, and says so."""
    if not c or c.get("tag") in (None, "ERROR"):
        return ""
    if c["tag"] == "UNREAD":
        return f" · COVER [UNREAD]: {c.get('why', '')}"
    kind = c["tag"] + (", warrant/convertible/ELOC shares" if c.get("issuable") else "")
    s = f" · COVER [{kind}] ({c.get('form')} filed {c.get('filed')})"
    if c.get("cover"):
        s += f': "{c["cover"][:520]}"'
    n, out = c.get("shares"), c.get("outstanding")
    if n and out:
        s += f" · {n:,.0f} sh = {c['pct']}% of {out:,.0f} outstanding (same document)"
    elif n:
        so = (((card or {}).get("figures") or {}).get("shares_out") or {}).get("value")
        if so:
            s += (f" · {n:,.0f} sh = {100 * n / so:.2f}% of {so:,.0f} outstanding "
                  f"(fincard dei count — the document prints none)")
        else:
            s += f" · {n:,.0f} sh (no outstanding count in the document or a fincard)"
    return s


def _load_or_build_card(tk):
    card = _j(NAMES / tk / "fincard.json", {})
    if card:
        return card
    sys.path.insert(0, str(HERE.parent / "valuation"))
    import fincard
    card = fincard.build(tk)
    (NAMES / tk).mkdir(parents=True, exist_ok=True)
    (NAMES / tk / "fincard.json").write_text(json.dumps(card, indent=1))
    return card


def _coded_gate(card, doc_shares=None):
    """G1 / G2-cfo / G3 of research/gate.py, read off the fincard, BEFORE pre-triage.
    G1 and G3 are gate.py's own tests (equity > 0; a usable share count and market cap, no
    open DOES-NOT-FOOT flag). G2 here is only its CFO leg, and it applies to every row, not
    only the compounder bar: the PM's instruction (scout.py-267) is that a warrant/PIPE
    resale of a CASH-BURNING issuer leaves on the shelf, and a TTM operating cash outflow
    is that fact in one number. A missing figure is 'not evaluated', never a fail."""
    F = (card or {}).get("figures") or {}
    D = (card or {}).get("derived") or {}
    gates = []
    eq = (F.get("equity") or {}).get("value")
    gates.append({"gate": "G1", "pass": None if eq is None else eq > 0,
                  "detail": "no equity figure" if eq is None else f"equity {eq:,.0f}"})
    cfo = (F.get("cfo") or {}).get("value")
    per = (F.get("cfo") or {}).get("period") or ""
    gates.append({"gate": "G2-cfo", "pass": None if cfo is None else cfo >= 0,
                  "detail": "no CFO figure" if cfo is None
                  else f"CFO {cfo:,.0f} ({per[:40]})" + (" — cash-burning issuer" if cfo < 0 else "")})
    shares = (F.get("shares_out") or {}).get("value")
    mcap = (D.get("market_cap") or {}).get("value")
    price = ((card or {}).get("price") or {}).get("value")
    src = ""
    if (not shares or shares <= 1) and doc_shares:
        # a registrant weeks past its IPO/de-SPAC has no dei cover count on companyfacts
        # yet — the registration statement being gated prints one, so use it, named
        shares, mcap, src = doc_shares, None, " (the registration statement's outstanding count)"
    if not mcap and shares and price:
        mcap = shares * price
    foot = [f for f in (card or {}).get("flags", []) if "DOES NOT FOOT" in f]
    if not shares or shares <= 1:
        g3 = (False, f"shares_out={shares!r} — not a usable share count")
    elif not mcap or mcap <= 0:
        g3 = (False, "market cap not resolvable from shares x price")
    elif foot:
        g3 = (False, f"open balance-sheet-gap: {foot[0][:120]}")
    else:
        g3 = (True, f"shares_out {shares:,.0f}{src}, market cap {mcap:,.0f}")
    gates.append({"gate": "G3", "pass": g3[0], "detail": g3[1]})
    failed = next((g for g in gates if g["pass"] is False), None)
    return {"verdict": "fail" if failed else "pass", "failed_gate": failed["gate"] if failed else None,
            "gates": gates, "market_cap": mcap}


_UNJUDGED = ("new", "pre_triaged", "enriching", "triaged", "triage_failed")


def cover_and_gate(items, now, cap=30):
    """Stage 0b for reg-effective rows: read the cover (once per row, 3 tries on a fetch
    error), then run the coded gate on rows no human has judged. A fail leaves the funnel
    as status 'gated' with the failed gate quoted — out of pre-triage AND out of vp.py's
    candidate_desk, which fills slots from pre_triaged rows. A RESALE/MIXED row that
    passes and was pre-scored blind before its cover existed goes back to 'new' so the
    pre-triage reads the verbatim cover. A human verdict (pm_reviewed/dropped/underwriting)
    only gains the cover as evidence; its status never moves. Returns counts."""
    n = {"read": 0, "resale": 0, "gated": 0, "rescore": 0, "errors": 0}
    todo = [it for it in items.values() if it.get("kind") == "reg-effective"
            and (not it.get("cover") or (it["cover"].get("tag") == "ERROR" and it["cover"].get("tries", 0) < 3))]
    todo.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    for it in todo[:cap] if cap else todo:
        parts = str(it.get("id", "")).split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            it["cover"] = {"tag": "UNREAD", "why": "no CIK on the row id"}
            continue
        try:
            c = _reg_cover(parts[1], parts[2])
        except Exception as ex:
            c = {"tag": "ERROR", "why": str(ex)[:120], "tries": (it.get("cover") or {}).get("tries", 0) + 1}
            n["errors"] += 1
        c["at"] = now
        it["cover"] = c
        n["read"] += 1
    for it in items.values():
        c = it.get("cover")
        if it.get("kind") != "reg-effective" or not c or c.get("tag") == "ERROR":
            continue
        tk = it.get("ticker")
        card = None
        if tk and "gate" not in it and it.get("status") in _UNJUDGED:
            try:
                card = _load_or_build_card(tk)
                g = _coded_gate(card, c.get("outstanding"))
            except (Exception, SystemExit) as ex:   # fincard.resolve_cik raises SystemExit
                g = {"verdict": "not evaluated", "why": f"fincard build failed: {str(ex)[:100]}"}
            g["at"] = now
            it["gate"] = g
            if g["verdict"] == "fail":
                fg = next(x for x in g["gates"] if x["gate"] == g["failed_gate"])
                it["status"] = "gated"
                it["pre"] = {"plausible": 0, "channel": "none",
                             "why": f"coded gate {fg['gate']} fail: {fg['detail']}"[:90]}
                n["gated"] += 1
            elif c.get("tag") in ("RESALE", "MIXED") and it.get("pre") and it["status"] != "new":
                it["status"] = "new"   # new evidence (the cover), not a re-derivation
                n["rescore"] += 1
        if c.get("tag") in ("RESALE", "MIXED"):
            n["resale"] += 1
        if "COVER [" not in str(it.get("detail", "")):
            base = str(it.get("detail", "")).replace(" — resale/selling-stockholder shelf or primary raise?", "")
            it["detail"] = base + _cover_text(c, card or (_j(NAMES / tk / "fincard.json", {}) if tk else {}))
    return n


# scout.py-273 (numbers 2026-09-26): _events() re-quotes a cannibal row's net cash from a
# newer fincard only while the screen still HITS the name — a row that fell off the screen
# (dropped/pm_reviewed, never revisited by the merge) keeps the screen's figure forever, so
# every later quarter's card rebuild can contradict it (GIII 379M vs card 521M, SIG 608M vs
# 527M; scout.py-082 hand-closed the previous 8 of the same class). Refresh it from the card
# on every run instead, keeping the superseded figure in the row so the PM's verdict still
# reads against what it was given. Detail text only — status and pm_note never move.
_NETCASH_RE = re.compile(r"net cash \$(-?[\d,]+(?:\.\d+)?)M", re.I)
_REQUOTE_NOTE_RE = re.compile(r" · net cash re-quoted from fincard built [^·]*$")


def refresh_cannibal_netcash(items):
    n = 0
    for it in items.values():
        if it.get("kind") != "cannibal-screen" or not it.get("ticker"):
            continue
        det = str(it.get("detail") or "")
        m = _NETCASH_RE.search(det)
        card = _j(NAMES / it["ticker"] / "fincard.json", {})
        cnc = ((card.get("derived") or {}).get("net_cash") or {}).get("value")
        if not m or not isinstance(cnc, (int, float)):
            continue
        claimed = float(m.group(1).replace(",", "")) * 1e6
        # C17's own tolerance (contract.py funnel_violations): same sign and within 10%
        if (claimed >= 0) == (cnc >= 0) and abs(claimed - cnc) <= 0.10 * max(abs(cnc), 1):
            continue
        prior = _REQUOTE_NOTE_RE.search(det)
        was = re.search(r"superseded \$(-?[\d,]+)M", prior.group(0)).group(1) if prior and "superseded $" in prior.group(0) \
            else m.group(1)
        det = _REQUOTE_NOTE_RE.sub("", det)
        det = det[:m.start()] + f"net cash ${cnc / 1e6:,.0f}M" + det[m.end():]
        it["detail"] = det + (f" · net cash re-quoted from fincard built {str(card.get('built', '?'))[:10]} "
                              f"(superseded ${was}M from the screen run this row was judged on)")
        n += 1
    return n


def _fincard_summary(tk):
    """scout.py-029: share_count_change_pct's own formula says "over 1365 days" — a span
    that varies by issuer with dei history and is almost never one year — but this printed
    only the bare number, so the triage model imported it into a sketch labeled "y/y" (a
    ~2.2x overstatement on LZ). Any derived figure whose formula carries a span now prints
    that span next to the value, so a multi-year change cannot read as an annual one."""
    card = _j(NAMES / tk / "fincard.json", {})
    if not card:
        return None, None
    D = card.get("derived", {})
    keys = ("market_cap", "enterprise_value", "net_cash", "fcf", "ev_over_fcf",
            "fcf_yield_pct", "revenue_growth_pct", "pe", "price_over_book",
            "share_count_change_pct", "debt_over_ebitda")
    lines = []
    for k in keys:
        if k not in D:
            continue
        entry = D[k]
        line = f"{k}={entry['value']:,.2f}"
        m = _SPAN_DAYS_RE.search(entry.get("formula") or "")
        if m:
            days = int(m.group(1))
            line += f" [span {days}d ({days / 365.25:.1f}y) — NOT y/y]"
        lines.append(line)
    flags = card.get("flags", [])
    txt = "; ".join(lines) + ("; FLAGS: " + " | ".join(f[:60] for f in flags[:3]) if flags else "")
    return txt, card


def run(max_pre=25, max_triage=8, max_cover=30):
    q = _j(CAND, {"_doc": "scout funnel — see scout.py", "items": {}})
    items = q.get("items", {})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    evs = _events()
    for e in evs:  # late-resolving fields (13D subjects, spin parents, 13F issuer matches) land on the existing row
        cur = items.get(e["id"])
        if cur and not cur.get("ticker") and e.get("ticker"):
            cur["ticker"], cur["detail"] = e["ticker"], e.get("detail") or cur.get("detail")
            if cur.get("status") == "pre_triaged":
                cur["status"] = "new"   # it can be judged on its merits now — re-score it
            elif cur.get("status") == "no_ticker":
                # it was blocked ONLY on the ticker, already scored plausible enough to
                # reach enrichment once — a resolver fix (matcher, spin-parent lookup)
                # unblocks it same as a fresh event would, not just events arriving after
                # the fix (scout.py-037).
                cur["status"] = "enriching"
        if cur and e.get("kind") == "reg-effective" and "RESALE SHELF confirmed" in (e.get("detail") or "") \
                and "RESALE SHELF confirmed" not in (cur.get("detail") or ""):
            # scout.py-215: local_read.py's verified seller quote usually lands AFTER this
            # row already exists and was pre-triaged blind ("shelf or primary raise?" with
            # no answer, e.g. GWHWW scored plausible:1 on 2026-09-18 before its S-3 had been
            # read). A newly-verified named seller + share count is new evidence, not a
            # re-derivation of an old verdict — carry it onto the card and let it be
            # re-scored, same as a ticker resolving unblocks a "no_ticker" row above. Only a
            # not-yet-human-judged row moves; pm_reviewed/dropped/underwriting stand.
            cur["detail"] = e["detail"]
            if cur.get("status") in ("new", "pre_triaged", "enriching", "triaged"):
                cur["status"] = "new"
        if cur and e.get("kind") in ("index-deletion", "spin-completed") and cur.get("detail") != e.get("detail"):
            # scout.py-261: the price vs the forced-sale print moves every day — the R-37
            # line on the row must be today's, never the day it was minted. Detail only.
            cur["detail"], cur["last_seen"] = e["detail"], now
        if cur and e.get("kind") == "cannibal-screen":
            # scout.py-056: a re-hit refreshes the screen's numbers and sighting count on
            # the SAME row — never status or pm_note. A dropped/pm_reviewed verdict must
            # survive every later run; only a human reopen (or a new event kind entirely)
            # should move it off that status.
            cur["date"], cur["detail"] = e["date"], e["detail"]
            cur["runs_on_screen"] = e.get("runs_on_screen")
            cur["bucket"] = e.get("bucket")   # scout.py-172: a ticker can move buckets run to run
            cur["last_seen"] = now
        # scout.py-172: the insider-cluster merge branch that used to live here (scout.py-081)
        # is dead now that _events() no longer mints that kind (STRATEGY-PROPOSAL-v3 §1)  —
        # any existing "insider-cluster" row just stops refreshing, which is correct: it was
        # demoted to a confirmer, not deleted from candidates.json.
    # a deletion the PM already carries by hand (pm:AMSF:...:sp600-deletion, or the cohort
    # row pm:SP600-DELETIONS whose ticker field lists five names) is not minted twice
    pm_idx = {t for i in items.values() if i.get("kind") == "index-deletion" and not str(i.get("id", "")).startswith("idxdel:")
              for t in str(i.get("ticker") or "").split()}
    pm_spin = {i.get("ticker") for i in items.values() if not str(i.get("id", "")).startswith("spindone:")
               and ("spin" in str(i.get("kind", "")) or ":spin" in str(i.get("id", "")))}
    new = [e for e in evs if e["id"] not in items
           and not (e.get("kind") == "index-deletion" and e.get("ticker") in pm_idx)
           and not (e.get("kind") == "spin-completed" and e.get("ticker") in pm_spin)]
    n_pre = n_tri = n_triaged_ok = 0
    for e in new:
        items[e["id"]] = {**e, "status": "new", "first_seen": now, "last_seen": now}
    # Stage 1: pre-triage newest-first, capped per run
    # scout.py-215 (pm, 2026-09-14): a channel that names a SELLER BY CONSTRUCTION --
    # reg-effective (S-1/S-3 resale/selling-stockholder shelf) and spin-registration (the
    # distribution itself is the seller's mechanism) -- passes G5 (STRATEGY-v3 §1/§7,
    # named seller + filing citation) on its face; 13D and cannibal-screen do not (a 13D
    # names a BUYER, a screen hit names a question). Under the per-run max_pre cap, a
    # newest-first-only queue can starve seller-named rows behind a larger day's worth of
    # 13D/screen rows -- QVCG's S-1 (37.6M sh, 75.2% of outstanding, four creditor funds
    # named) never minted a scout row while five buyer-mechanism leads were reviewed and
    # all failed G5. Stable two-pass sort: newest-first within each group, seller-named
    # group promoted ahead of the rest.
    _SELLER_NAMED_KINDS = {"reg-effective", "spin-registration", "index-deletion", "spin-completed"}
    # Stage 0b (scout.py-267): reg-effective rows get their registration statement's cover
    # read and the coded G1/G2-cfo/G3 gate BEFORE the model sees them
    cg = cover_and_gate(items, now, cap=max_cover)
    refresh_cannibal_netcash(items)
    todo = [i for i in items.values() if i["status"] == "new"]
    todo.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    todo.sort(key=lambda x: x.get("kind") not in _SELLER_NAMED_KINDS)
    for it in todo[:max_pre]:
        v = ask_json(PRE_PROMPT + "\n\nEVENT: " + json.dumps(
            {k: it.get(k) for k in ("kind", "ticker", "issuer", "date", "detail")}),
            num_predict=260, job="scout pre-triage")
        # the trigger must be VERBATIM from the detail line — a score with no quotable trigger
        # is the unauditable yes/no the 2026-09-07 audit flagged; code checks it, not the model
        trig = str(v.get("trigger", ""))[:120] if isinstance(v, dict) else ""
        trig_ok = bool(trig) and " ".join(trig.lower().split()) in " ".join(str(it.get("detail", "")).lower().split())
        it["pre"] = {"plausible": int(v.get("plausible", 0)) if str(v.get("plausible", "")).isdigit() else 0,
                     "why": str(v.get("why", ""))[:90], "channel": str(v.get("channel", ""))[:40],
                     "trigger": trig if trig_ok else "", "trigger_verified": trig_ok} \
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
        it["status"] = "enriching" if (pl >= 4 or (pl >= 3 and it.get("ticker"))) else "pre_triaged"  # scout.py-172: pl>=5 was redundant with pl>=4
        n_pre += 1
    # Re-check the standing backlog against the CURRENT gate. Scores are already stored,
    # so this costs no model call — and without it a gate change only ever applies to
    # events that arrive after it, leaving everything already collected dead forever.
    for it in items.values():
        if it["status"] == "pre_triaged":
            pl = (it.get("pre") or {}).get("plausible") or 0
            if pl >= 4 or (pl >= 3 and it.get("ticker")):
                it["status"] = "enriching"

    # signals pipeline-health pass (2026-09-15): enrich_failed had no reader anywhere
    # (grep confirms) and no retry -- a fincard 404 at enrich time (an unresolvable
    # ticker, or SEC's endpoint hiccuping) permanently hid the lead from every downstream
    # view: the PM's queue, desk.py, and the signals new-names table alike. TREO/WCCB/FRTT
    # were all live, un-reviewed candidates as of 2026-09-12 and had silently become
    # invisible dead rows by 2026-09-15 for exactly this reason. Retry weekly: often
    # enough that a transient/now-fixed resolver issue clears, rare enough that a
    # genuinely unresolvable ticker (OTC-only, no SEC XBRL) doesn't hammer the same 404
    # every run.
    _retry_cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    for it in items.values():
        if it["status"] == "enrich_failed" and str(it.get("last_seen", "")) < _retry_cutoff:
            it["status"] = "enriching"

    # Stage 2+3: enrich + full triage for the plausible, capped per run
    enrich_backlog = [i for i in items.values() if i["status"] == "enriching"]
    for it in enrich_backlog[:max_triage]:
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
            # (Exception, SystemExit), not Exception (hunt 2026-09-05): fincard.resolve_cik
            # raises SystemExit — a BaseException — when SEC's company_tickers.json has no
            # entry for the symbol. Scout's tickers come from news and filing feeds, which
            # is exactly where an unresolvable symbol shows up, and a bare `except Exception`
            # let that one row kill the whole scout run instead of marking itself
            # enrich_failed and moving on.
            except (Exception, SystemExit) as ex:
                it["status"] = "enrich_failed"
                it["error"] = str(ex)[:120]
                continue
        # scout.py-061: "no live price" (fincard.py) on a 13D subject is the other half of
        # the interval/tender-offer fund signature — a listed activist target always has one.
        # Coded, not left for the local model to notice as free-text red_flags: it never
        # reaches the TRIAGE call, which would otherwise spend the LLM read on an event that
        # cannot be a listed-discount thesis.
        if it["kind"] == "13D" and card and any("no live price" in f for f in card.get("flags", [])):
            it["status"] = "pre_triaged"
            it["pre"] = {"plausible": 0,
                         "why": "no live quote — interval/tender-offer fund, not a listed discount",
                         "channel": "none"}
            continue
        v = ask_json(TRIAGE_PROMPT + "\n\nEVENT: " + json.dumps(
            {k: it.get(k) for k in ("kind", "ticker", "issuer", "date", "detail")})
            + "\n\nFINANCIAL CARD SUMMARY: " + (summary or "unavailable"),
            num_predict=900, think=True, job="scout triage")
        if isinstance(v, dict) and str(v.get("score", "")).lstrip("-").isdigit():
            sketch = str(v.get("sketch", ""))[:500]
            bad = _unsupported_figures(sketch, it.get("detail", ""))
            it["triage"] = {"score": int(v["score"]), "channel": str(v.get("channel", ""))[:40],
                            "sketch": sketch,
                            "variant_needed": str(v.get("variant_needed", ""))[:200],
                            "red_flags": str(v.get("red_flags", ""))[:200], "at": now}
            if bad:
                it["triage"]["unsupported_figures"] = bad   # scout.py-029: not in coded detail
            it["status"] = "triaged"
            n_triaged_ok += 1
        else:
            it["status"] = "triage_failed"
        n_tri += 1
    q["items"] = items
    q["scanned_at"] = now
    CAND.write_text(json.dumps(q, indent=1))
    # scout.py-172: backlog-vs-processed per stage, so a growing "in" with a flat "out"
    # (the queue falling behind max_pre/max_triage's per-run cap) is visible instead of
    # looking identical to "nothing new arrived."
    funnel_record("scout:new", len(evs), len(new))
    funnel_record("scout:pre-triaged", len(todo), n_pre)
    funnel_record("scout:enriched", len(enrich_backlog), n_tri)
    funnel_record("scout:triaged", n_tri, n_triaged_ok)
    # A row can reach status=triaged without a "triage" dict if a PM session hand-edits
    # status back onto a row that never ran the code triage stage (e.g. reopening a
    # pre_triaged/pm_reviewed candidate) — this crashed every run for a full market day
    # on 2026-08-24 (KeyError: 'triage'), 15 times, after the write above had already
    # succeeded — only this display line was ever at risk, but a crash here still kills
    # the cron job with a non-zero exit every run until the offending row's status
    # changes. Require a real "triage" dict to enter the display, same gate the sort key
    # implicitly assumed but never enforced (scout.py-crash-2026-08-26).
    top = sorted((i for i in items.values() if i["status"] == "triaged" and "triage" in i),
                 key=lambda x: -x["triage"]["score"])[:5]
    print(f"{now} scout: {len(new)} new events · {n_pre} pre-triaged · {n_tri} triaged · "
          f"reg covers read {cg['read']} ({cg['resale']} resale/mixed on file, {cg['gated']} gated, "
          f"{cg['rescore']} re-queued, {cg['errors']} fetch errors) · "
          f"queue top: {[(t.get('ticker'), t['triage']['score']) for t in top]}")


def covers(cap=None):
    """The code-only stages alone, no model (`scout.py covers [N]`): 0b reg-effective
    cover + gate, and the cannibal net-cash re-quote (scout.py-273)."""
    q = _j(CAND, {"items": {}})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    n = cover_and_gate(q["items"], now, cap=cap)
    n["netcash_requoted"] = refresh_cannibal_netcash(q["items"])
    q["scanned_at"] = now
    CAND.write_text(json.dumps(q, indent=1))
    print(f"{now} scout covers: {n}")


def list_items():
    q = _j(CAND, {})
    rows = sorted(q.get("items", {}).values(),
                  key=lambda x: -(x.get("triage", {}).get("score") or x.get("pre", {}).get("plausible", 0) or 0))
    for i in rows[:25]:
        t = i.get("triage") or {}
        p = i.get("pre") or {}
        score = t.get("score", f"pre:{p.get('plausible', '?')}")
        flag = " [UNSUPPORTED FIGURES: " + ", ".join(t["unsupported_figures"]) + "]" \
            if t.get("unsupported_figures") else ""
        print(f"[{i['status']:12s}] {score!s:>6} {str(i.get('ticker') or '—'):6s} {i['kind']:16s} "
              f"{(t.get('sketch') or p.get('why') or i.get('detail', ''))[:90]}{flag}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        run()
    elif sys.argv[1:2] == ["list"]:
        list_items()
    elif sys.argv[1:2] == ["covers"]:
        covers(int(sys.argv[2]) if sys.argv[2:3] else None)
    else:
        sys.exit("usage: scout.py run | scout.py list | scout.py covers [N]")
