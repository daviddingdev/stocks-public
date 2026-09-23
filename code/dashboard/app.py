#!/usr/bin/env python3
"""
Stocks research dashboard — local, interactive, design-forward SPA.

Home = live portfolio overview (holdings vs. watchlist, KPIs, allocation, filterable
"what's new" digest). Ticker pages: live quote + chart + key-stats grid (52wk range,
your position) + tabbed Overview / Filings / News / Research library. Sidebar is
entity-based (companies, not files) with a typeahead search ("/" to focus) over the
SEC ticker map. Research docs render with breadcrumbs + a sticky table of contents.
Finnhub quotes/news; EDGAR filings; yfinance fallback.

View: http://<host-ip>:8787
"""
import collections
import csv as csvmod
import datetime as dt
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

# serve.sh runs this file as __main__; the page modules' lazy `import app` (dip_page, jpm_page,
# industries_page) then loaded it a SECOND time as `app`: a second quote cache nobody warms, and
# every page module registered again on a second Flask app nobody serves (2026-09-23). One module.
if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules["__main__"])

import markdown
import requests
from flask import Flask, abort, jsonify, request

ROOT = Path("~/Stocks").expanduser().resolve()
CONF = ROOT / "_engine" / "config"
sys.path.insert(0, str(ROOT / "_engine" / "research"))
import runner  # headless Claude launcher (research + daily recommendation)
PORT = 8787
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # SEC contact identity, config-driven
app = Flask(__name__)
MD_EXTS = ["tables", "fenced_code", "toc", "sane_lists", "attr_list"]

LABELS = {
    "FINAL-REPORT.md": "Final report", "soft-research-dossier.md": "Thesis & dossier",
    "imvt1402-valuation-dossier.md": "IMVT-1402 valuation", "path-to-10-valuation.md": "Path to $10",
    "trade-playbook.md": "Trade playbook", "fundamental-analysis.md": "Fundamental analysis",
    "valuation-model.md": "Valuation model",
    "governance-insiders.md": "Governance & insiders", "pipeline-partners.md": "Pipeline & partners",
    "approval-probability.md": "Approval probability", "ip-litigation.md": "IP & litigation",
    "historical-financials.md": "Historical financials", "imvt1402-science.md": "IMVT-1402 science",
    "imvt1402-market.md": "IMVT-1402 market", "imvt1402-omniab-economics.md": "IMVT-1402 economics",
    "tracker-log.md": "Tracker log", "model.csv": "Financial snapshot", "quarterly.csv": "Quarterly series",
    "ARCHITECTURE.md": "Architecture", "EVALUATION-FRAMEWORK.md": "Evaluation framework",
    "COMMS.md": "Comms protocol", "RUNBOOK.md": "Research runbook",
}
ORDER = list(LABELS.keys())
GROUPS = [("Reports & analysis", "analysis"), ("Updates", "analysis/updates"),
          ("Deep research", "research"), ("Financials", "financials")]
KEY = ("revenue", "net_loss", "net loss", "operating_cash", "cash_plus", "r&d", "operating_loss")

# ---------- config ----------
def _json(name, default):
    f = CONF / name
    try:
        return json.loads(f.read_text()) if f.exists() else default
    except Exception:
        return default

def load_key(name):
    return _json("keys.json", {}).get(name, "")

# ---------- books (2026-09-11) ----------
# One dashboard, several books. The BROKERA book keeps the legacy paths (config/*.json);
# every other book lives in config/<slug>/ (see _engine/books.py). The CURRENT book is
# request-scoped: `?book=<slug>` on any API call, or the page route (/dip). The page JS
# appends book= to every /api/ fetch while a non-BROKERA book is on screen, so the loaders
# written for the BROKERA home (pfhistory, transactions, lots, lifetime, external…) serve
# any book without knowing it. Cache keys carry the slug for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import books as _books  # noqa: E402

def _book():
    from flask import g, has_request_context
    if not has_request_context():
        return "brokera"
    b = getattr(g, "book", None)
    if b is None:
        b = (request.args.get("book") or "").strip().lower()
        if not b:
            b = "dip" if (request.path == "/dip" or request.path.startswith("/dip/")) else "brokera"
        if b not in _books.slugs():
            b = "brokera"
        g.book = b
    return b

def _bjson(name, default):
    f = _books.book_dir(_book()) / name
    try:
        return json.loads(f.read_text()) if f.exists() else default
    except Exception:
        return default

def read_positions():
    return _bjson("positions.json", {})

def read_account():
    return _bjson("account.json", {})

def read_external():
    """Off-feed holdings that belong in the book's allocation: the BROKERA book's
    external.json, or a book's hand-marked private.json (name/value/category/…)."""
    ext = _bjson("external.json", None)
    return ext if ext is not None else _bjson("private.json", [])

def _book_aid(st):
    """The AggregatorA account behind the current book's activity feed. A book with
    several accounts reads the first (neither book has more than one, 2026-09-11);
    a book with none falls back to the user's first account, the pre-registry rule."""
    ids = (_books.load().get(_book()) or {}).get("accounts") or []
    if ids:
        return ids[0]
    u = _st_user(st); qp = {"userId": u["userId"], "userSecret": u["userSecret"]}
    return st.account_information.list_user_accounts(query_params=qp).body[0]["id"]

def watchlist():
    f = CONF / "watchlist.txt"
    if not f.exists():
        return []
    return [l.strip().upper() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]

# ---------- ticker resolution & data ----------
_TMAP = {}
_TNAMES = {}
def ticker_map():
    global _TMAP, _TNAMES
    if _TMAP:
        return _TMAP
    f = CONF / "company_tickers.json"
    if not f.exists():
        try:
            f.write_text(requests.get("https://www.sec.gov/files/company_tickers.json", headers=UA, timeout=20).text)
        except Exception:
            return {}
    try:
        d = json.loads(f.read_text())
        _TMAP = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in d.values()}
        _TNAMES = {v["ticker"].upper(): (v.get("title") or "").title() for v in d.values()}
    except Exception:
        _TMAP = {}
    return _TMAP

def ticker_names():
    ticker_map()
    return _TNAMES

def cik_of(sym):
    return ticker_map().get(sym.upper())

_CACHE = {}
_CACHE_LOCKS = {}
_CACHE_GUARD = threading.Lock()
def _cache_lock(k):
    with _CACHE_GUARD:
        return _CACHE_LOCKS.setdefault(k, threading.Lock())

class _Failed(list):
    """What a fetch returns when its source failed, in place of its empty value (an empty list;
    _FailedDict for a dict). The page shows it like any empty answer, but cached() owes the key a
    retry — retry_s, then doubling, retry_tries times — instead of trusting it for a whole TTL: off-
    market a TTL runs to the next open, and on the slow link Yahoo answers part of a batch with
    "possibly delisted" (13 of 30 names in the post-close pass, 2026-09-23). A later success clears it."""


class _FailedDict(dict):
    __doc__ = _Failed.__doc__


_CRETRY = {}   # cache key -> (failures in a row, epoch its retry falls due; inf = given up till its TTL)


def _note_fetch(k, v):
    if isinstance(v, (_Failed, _FailedDict)):
        n = _CRETRY.get(k, (0, 0.0))[0] + 1
        _CRETRY[k] = (n, time.time() + LIVE["retry_s"] * 2 ** (n - 1) if n <= LIVE["retry_tries"] else float("inf"))
    else:
        _CRETRY.pop(k, None)


def _fresh_hit(k, hit, ttl):
    return bool(hit) and time.time() - hit[0] < ttl and time.time() < _CRETRY.get(k, (0, float("inf")))[1]


def cached(k, ttl, fn, stale=False):
    """TTL cache, single-flight: a second caller arriving mid-fetch waits for the first
    result instead of fetching again (a phone and a laptop opening together used to pay
    every upstream call twice). stale=True hands back the expired value at once and
    refreshes it on a background thread — for panes that are minutes-old by nature (the
    digest, the 1y perf table), never for broker state or a live price (David 2026-09-03:
    "data feels slow"); _quote() has its own bounded rule for prices (_quote_window). A value
    that marks its fetch as failed (_Failed) is retried sooner than its TTL."""
    hit = _CACHE.get(k)
    if _fresh_hit(k, hit, ttl):
        return hit[1]
    lock = _cache_lock(k)
    if stale and hit:
        if lock.acquire(blocking=False):
            def refresh():
                try:
                    v = fn()
                    _CACHE[k] = (time.time(), v)
                    _note_fetch(k, v)
                finally:
                    lock.release()
            threading.Thread(target=refresh, daemon=True).start()
        return hit[1]
    with lock:
        hit = _CACHE.get(k)
        if _fresh_hit(k, hit, ttl):
            return hit[1]
        v = fn()
        _CACHE[k] = (time.time(), v)
        _note_fetch(k, v)
        return v

# ---------- live data: what refreshes when (David, 2026-09-23) ----------
# "for stocks i own ofcourse as real time as possible, but other stuff doesn't need to be so
# strict and can be like every 5 min during market hours and off-market don't need to reload
# anything." Every cadence the dashboard keeps for market data is in this one table; the plain-
# English version is the top of INTERACTIVITY.md ("Live data: what refreshes when").
#
#   market hours  owned names (every book: BROKERA, DIP, the BrokerB agent) re-quote every
#                 owned_every() s — owned_s, lengthened if the owned set outgrows the budget —
#                 and are never served older than owned_max_s; everything else ~every other_s.
#   off-market    nothing reloads. One pass settle_s after the close (the closing prints)
#                 refreshes every tracked name and pane, then nothing until the next open, whose
#                 first pass refreshes everything. Weekends and NYSE holidays: nothing.
#
# Finnhub's free tier allows 60 calls/min per key, shared with feeds.py (30-min cron, self-paced
# at ~55/min while it runs) and with whoever opens a ticker page nobody tracks. The warmer keeps
# the dashboard's steady state inside finnhub_budget. With the set on 2026-09-23 (8 owned, 22
# other tracked names):
#   owned quotes    8 × 60/15              = 32.0 /min
#   other quotes   22 × 60/(300 − 30)      =  4.9
#   company news   30 × 60/600             =  3.0
#   cap + 52-week  30 × 2 × 60/21600       =  0.2
#   earnings date  30 × 60/86400           =  0.0
#                                            40.1 /min of a 45 budget and the 60 cap
# finnhub_per_min() is that sum and owned_every() solves it for the owned cadence, so a bigger
# owned set lengthens it on its own (12 owned → 20 s).
#
# That is the steady state; the open is not. At 9:30 every price is 17.5 h old and every cap/52-
# week range is past meta_s, and a warmer that paid for all of it at once ran 69 calls in its first
# minute (measured on a virtual clock, 2026-09-23). So in market hours the owned re-quotes are a
# RESERVATION (owned_beat(): 8 × 4 = 32 a minute) held back before anything else is paid for, and
# every other background call — other quotes, then cap/52-week renewals, then news and earnings —
# shares what is left (fh_room(): 13 a minute at the open). A call that does not fit waits: the
# price warmer defers it a tick, the pane warmer waits in _fh_wait and skips it if no room comes.
# The open then catches up over a few minutes inside the budget. A page's own call never waits,
# and neither does an owned re-quote. Not modelled here: feeds.py, whose */30 cron starts at 9:30
# ET exactly and runs ~54/min on the same key — the one collision this budget cannot see.
LIVE = {
    "tick_s": 5,            # the price warmer wakes this often; it fetches only what is due
    "panes_tick_s": 30,     # the pane warmer (perf, digest, news, filings, charts, broker activity)
    "owned_s": 15,          # owned names: re-quoted this often in market hours (a floor, see above)
    "owned_max_s": 30,      # ...and never served older than this in market hours
    "other_s": 300,         # everything else: never served older than this in market hours, and
    "other_lead_s": 30,     # ...re-quoted this much before it gets there, so no page waits on it
    "news_s": 600,          # filings, company news and the digest
    "activity_s": 600,      # AggregatorA activity: transactions, lots
    "chart_s": 1800,        # close series: price charts, sparklines, the portfolio chart
    "slow_s": 3600,         # company profile, lifetime P&L, the calendar
    "meta_s": 6 * 3600,     # market cap + 52-week range
    "earn_s": 86400,        # next earnings date
    "sync_s": 900,          # AggregatorA autoSync on page open, market hours (off-market: only when
                            # the snapshot predates the last close)
    "settle_s": 300,        # the one post-close pass runs this long after the bell
    "retry_s": 60,          # a refused or unpriced quote (_quote_backoff) and a failed pane fetch
    "retry_tries": 3,       # ...(_Failed) are retried after this, doubling, this many times
    "finnhub_cap": 60,      # Finnhub free tier, calls/min per key
    "finnhub_budget": 45,   # the dashboard's steady share of it
}

# NYSE 1:00pm ET closes. asof.market_open() knows neither these nor holidays (it decides whether
# to EXPECT fresh market data, never a trade), so market() takes them away from it: the holidays
# from loop.MARKET_HOLIDAYS — the trade cron's own table, so the two cannot disagree — and the
# early closes from here. Hand-kept: add a year when loop.py adds one.
EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}
_NY = ZoneInfo("America/New_York")
_HOLIDAYS = []


def _asof():
    p = str(ROOT / "_engine" / "agent")
    if p not in sys.path:
        sys.path.insert(0, p)
    import asof
    return asof


def _holidays():
    if not _HOLIDAYS:
        try:
            _asof()   # agent/ on the path
            from loop import MARKET_HOLIDAYS
            _HOLIDAYS.append(frozenset(MARKET_HOLIDAYS))
        except Exception:
            _HOLIDAYS.append(frozenset())
    return _HOLIDAYS[0]


def _session(d):
    """(open, close) of the NYSE regular session on ET date d, or None: a weekend or a holiday."""
    if d.weekday() >= 5 or d.isoformat() in _holidays():
        return None
    close = dt.time(13) if d.isoformat() in EARLY_CLOSES else dt.time(16)
    return dt.datetime.combine(d, dt.time(9, 30), _NY), dt.datetime.combine(d, close, _NY)


def market(now=None):
    """Is the market open, and what changes next: the ONE answer the warmers, every market-data
    TTL and the page script (MKT, from market_client()) share.
      open     asof.market_open (9:30–16:00 ET weekdays, the desk's clock) minus NYSE holidays
               and the hours after a 1pm close
      close    epoch of the latest session close at or before now
      settled  epoch of the latest close + settle_s at or before now: off-market, anything
               fetched since this moment is final until the next open
      pending  closed, and the latest close's settle point not reached yet
      next     epoch of the next change the page acts on: a minute past the close while open, a
               minute past the settle point while pending, the next open otherwise"""
    now = now or dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(_NY)
    day = et.date().isoformat()
    try:
        is_open = bool(_asof().market_open(now))
    except Exception:
        s = _session(et.date())
        is_open = bool(s and s[0] <= et <= s[1] + dt.timedelta(seconds=59))
    if day in _holidays() or (day in EARLY_CLOSES and (et.hour, et.minute) > (13, 0)):
        is_open = False
    closes, nxt_open = [], None
    for k in range(15):
        s = _session(et.date() - dt.timedelta(days=k))
        if s and s[1] <= et:
            closes.append(s[1])
            if len(closes) == 2:
                break
    for k in range(15):
        s = _session(et.date() + dt.timedelta(days=k))
        if s and s[0] > et:
            nxt_open = s[0]
            break
    settle = dt.timedelta(seconds=LIVE["settle_s"])
    settled = next((c + settle for c in closes if c + settle <= et), None)
    pending = not is_open and bool(closes) and closes[0] + settle > et
    if is_open:
        today = _session(et.date())
        nxt = (today[1] if today else et) + dt.timedelta(seconds=60)
    elif pending:
        nxt = closes[0] + settle + dt.timedelta(seconds=60)
    else:
        nxt = (nxt_open or et + dt.timedelta(hours=1)) + dt.timedelta(seconds=5)
    return {"open": is_open, "pending": pending, "close": closes[0].timestamp() if closes else 0.0,
            "settled": settled.timestamp() if settled else 0.0, "next": nxt.timestamp(),
            "now": now.timestamp()}


def _ttl(base, now=None):
    """A market-data cache TTL under the policy: `base` in market hours; off-market, the time back
    to the settle point — anything fetched since (the post-close pass) is final until the open,
    anything older is fetched once."""
    m = market(now)
    if m["open"] or not m["settled"]:
        return base
    return max(0.0, m["now"] - m["settled"])


_SETS = {"at": 0.0, "owned": frozenset(), "tracked": frozenset()}


def _sets():
    """(owned, tracked): held_all() and tracked_tickers(), re-read at most once a tick — _quote()
    asks per name, and both walk every book and the research tree."""
    if time.time() - _SETS["at"] > LIVE["tick_s"]:
        try:
            owned = frozenset(held_all())
            _SETS.update(owned=owned, tracked=frozenset(tracked_tickers()) | owned)
        except Exception:
            pass
        _SETS["at"] = time.time()
    return _SETS["owned"], _SETS["tracked"]


def finnhub_per_min(owned_s, n_owned, n_other):
    """The dashboard's steady Finnhub calls a minute in market hours — the sum in LIVE's comment."""
    n = n_owned + n_other
    return (n_owned * 60 / owned_s + n_other * 60 / (LIVE["other_s"] - LIVE["other_lead_s"])
            + n * 60 / LIVE["news_s"] + n * 2 * 60 / LIVE["meta_s"] + n * 60 / LIVE["earn_s"])


def owned_every(n_owned=None, n_other=None):
    """Seconds between re-quotes of each owned name in market hours: owned_s, or longer when that
    would put the dashboard over its Finnhub budget — the owned set then shares what is left once
    everything else is paid for (and falls back to other_s if nothing is)."""
    if n_owned is None:
        owned, tracked = _sets()
        n_owned, n_other = len(owned), len(tracked - owned)
    if not n_owned:
        return LIVE["owned_s"]
    room = LIVE["finnhub_budget"] - finnhub_per_min(float("inf"), n_owned, n_other)
    if room <= 0:
        return LIVE["other_s"]
    return max(LIVE["owned_s"], math.ceil(n_owned * 60 / room))


def _post_close_done(m, now=None):
    """Off-market: whether the server's pass for the latest close is over — no price is still owed
    (a name in its retry backoff does not count) and the 1y table has been rebuilt since the settle
    point, or has given up retrying. What the page's one final pass waits for. Never raises."""
    if m["open"] or not m["settled"]:
        return True
    try:
        if _quotes_due(now):
            return False
        hit = _CACHE.get("perf")
        return bool(hit) and (hit[0] >= m["settled"] or _CRETRY.get("perf", (0, 0.0))[1] == float("inf"))
    except Exception:
        return True


def market_client(now=None):
    """What the page script runs its timers on (MKT in the boot script and on the Holdings list).
    `pending` also covers the server's own post-close pass: while it runs (the price warmer spreads
    the closing prints over the budget; perf is a slow Yahoo batch) the page asks again each minute,
    and its one final pass reads what that pass wrote, not the pre-close values."""
    m = market(now)
    pending, nxt = m["pending"], m["next"]
    if not m["open"] and not pending and not _post_close_done(m, now):
        pending, nxt = True, m["now"] + 60
    return {"open": m["open"], "pending": pending, "close": int(m["close"]),
            "next_s": max(1, int(nxt - m["now"])), "owned_s": owned_every(),
            "other_s": LIVE["other_s"], "sync_s": LIVE["sync_s"], "at": int(m["now"])}


# Upstream calls this process made, by kind (read on /api/live), and Finnhub's rolling minute:
# (monotonic time, whether it was an owned name's re-quote — the calls owned_beat() reserves).
_UPSTREAM = collections.Counter()
_FH_CALLS = collections.deque()
_FH_LOCK = threading.Lock()


def _up(kind):
    _UPSTREAM[kind] += 1


def _fh_note(kind, t=None):
    """Count one Finnhub call; every Finnhub call site in this file notes itself (a quote names
    its ticker, so an owned re-quote is told apart from everything else)."""
    _up("finnhub:" + kind)
    owned = kind == "quote" and t in _sets()[0]
    with _FH_LOCK:
        _FH_CALLS.append((time.monotonic(), owned))


def fh_last_minute(owned=None):
    """Finnhub calls in the rolling minute (strictly the last 60 s): all of them, or with
    owned=True only the owned names' quotes."""
    with _FH_LOCK:
        cut = time.monotonic() - 60
        while _FH_CALLS and _FH_CALLS[0][0] <= cut:
            _FH_CALLS.popleft()
        return sum(1 for _, o in _FH_CALLS if o) if owned else len(_FH_CALLS)


def owned_beat(m=None):
    """In market hours, the Finnhub calls a rolling minute holds for the owned names' re-quotes
    (n owned × ceil(60 / owned_every())) — reserved before any other background call is paid for.
    Off-market there is no beat: 0."""
    m = m or market()
    n = len(_sets()[0])
    return n * math.ceil(60 / owned_every()) if m["open"] and n else 0


def fh_room(m=None):
    """Finnhub calls the warmers may still make in this rolling minute, beyond the owned beat.
    Market hours: finnhub_budget, less the owned reservation (or what the owned re-quotes in the
    window actually used, if more), less every other call in the window. Off-market: the budget
    less every call in it."""
    m = m or market()
    total = fh_last_minute()
    if not m["open"]:
        return LIVE["finnhub_budget"] - total
    mine = fh_last_minute(owned=True)
    return LIVE["finnhub_budget"] - max(owned_beat(m), mine) - (total - mine)


def _fh_wait(n=1, deadline=90):
    """A pane warmer's background call waits here for fh_room() — never while holding a cache
    lock, so a page asking for the same name is not stuck behind it. True once there is room;
    False at the deadline, and the caller skips the call (its entry stays due for the next pass)
    rather than spend past the budget — at the open the price warmer has first claim for a few
    minutes."""
    end = time.monotonic() + deadline
    while fh_room() < n:
        if time.monotonic() >= end:
            return False
        time.sleep(1)
    return True

def edgar_filings(cik, n=20):
    def fetch():
        _up("edgar:filings")
        try:
            rec = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=UA, timeout=20).json()["filings"]["recent"]
        except Exception:
            return _Failed()
        desc = rec.get("primaryDocDescription", [""] * len(rec["form"]))
        out = []
        for i in range(min(n, len(rec["form"]))):
            acc = rec["accessionNumber"][i].replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{rec['primaryDocument'][i]}"
            d = desc[i] if i < len(desc) else ""
            form = rec["form"][i]
            out.append({"form": form, "date": rec["filingDate"][i], "desc": d, "url": url,
                        "material": form.startswith("8-K"),
                        "earnings": form.startswith("8-K") and ("result" in d.lower() or "earning" in d.lower())})
        return out
    return cached(f"edgar:{cik}", _ttl(LIVE["news_s"]), fetch)

def finnhub_news(sym):
    key = load_key("finnhub")
    if not key:
        return None
    def fetch():
        to = dt.date.today(); frm = to - dt.timedelta(21)
        _fh_note("news")
        try:
            return requests.get("https://finnhub.io/api/v1/company-news",
                                params={"symbol": sym.upper(), "from": str(frm), "to": str(to), "token": key}, timeout=15).json()[:20]
        except Exception:   # a 429 answers a dict, which cannot be sliced: lands here too
            return _Failed()
    return cached(f"news:{sym}", _ttl(LIVE["news_s"]), fetch)

def next_earnings(sym):
    key = load_key("finnhub")
    if not key:
        return None
    def fetch():
        _fh_note("earnings")
        try:
            to = dt.date.today() + dt.timedelta(120)
            r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                             params={"from": str(dt.date.today()), "to": str(to), "symbol": sym.upper(), "token": key},
                             timeout=10).json()
            ds = sorted(e.get("date") for e in (r.get("earningsCalendar") or []) if e.get("date"))
            return ds[0] if ds else None
        except Exception:
            return _Failed()   # falsy, like None
    return cached(f"earn:{sym}", _ttl(LIVE["earn_s"]), fetch)

def profile(sym):
    def fetch():
        _up("yahoo:profile")
        try:
            import yfinance as yf
            i = yf.Ticker(sym).info
            return {"name": i.get("shortName") or i.get("longName") or sym, "sector": i.get("sector"),
                    "industry": i.get("industry"), "country": i.get("country"), "summary": i.get("longBusinessSummary")}
        except Exception:
            return _FailedDict()
    return cached(f"prof:{sym}", _ttl(LIVE["slow_s"]), fetch)

# ---------- filesystem nav ----------
def order_key(f):
    return (ORDER.index(f.name) if f.name in ORDER else 999, f.name.lower())

def label(p):
    if p.name in LABELS:
        return LABELS[p.name]
    if p.suffix == ".md":
        for line in p.read_text(errors="ignore").splitlines():
            if line.startswith("# "):
                t = line[2:].strip()
                return t.split("—")[-1].strip() if "—" in t else t
    return p.stem.replace("-", " ").replace("_", " ").capitalize()

def safe(rel):
    p = (ROOT / rel).resolve()
    if ROOT not in p.parents and p != ROOT:
        abort(403)
    return p

def companies():
    return sorted(d for d in ROOT.iterdir() if d.is_dir() and not d.name.startswith((".", "_")))

def ticker_of(cdir):
    return cdir.name.split("-")[-1].upper()

def company_folder(sym):
    for c in companies():
        if ticker_of(c) == sym.upper():
            return c
    return None

def company_groups(d):
    out = {}
    for glabel, sub in GROUPS:
        sd = d / sub
        if not sd.exists():
            continue
        files = [f for f in sd.iterdir() if f.suffix.lower() in (".md", ".csv") and not f.name.startswith(".")]
        if files:
            out[glabel] = sorted(files, key=order_key)
    return out

def boards():
    bd = ROOT / "_engine" / "candidate-boards"
    return sorted(bd.glob("board_*.md"), reverse=True) if bd.exists() else []

def sysdocs():
    e = ROOT / "_engine"
    return [e / r for r in ["INFORMATION-ARCHITECTURE.md", "ARCHITECTURE.md", "agent/COMMS.md",
            "research/EVALUATION-FRAMEWORK.md", "research/RUNBOOK.md"] if (e / r).exists()]

def a(p):
    rel = p.relative_to(ROOT)
    return f"<a class='leaf' data-path='{rel}' href='/view?path={rel}'>{html.escape(label(p))}</a>"

I_HOME = "<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M2.5 6.5 8 2l5.5 4.5V13a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1z'/><path d='M6 14v-4h4v4'/></svg>"
I_SEARCH = "<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round'><circle cx='7' cy='7' r='4.5'/><path d='m13.5 13.5-3.2-3.2'/></svg>"
I_REFRESH = "<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M13.5 8a5.5 5.5 0 1 1-1.6-3.9'/><path d='M13.5 1.5v3h-3'/></svg>"
I_DOC = "<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M9.5 1.5h-5a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V4.5z'/><path d='M9.5 1.5v3h3'/></svg>"
I_LOGO = ("<svg class=logo viewBox='0 0 20 20'><rect x='2' y='11' width='3.2' height='7' rx='1'/>"
          "<rect x='8.4' y='7' width='3.2' height='11' rx='1'/><rect x='14.8' y='2' width='3.2' height='16' rx='1'/></svg>")
I_SPARK = ("<svg class=ic viewBox='0 0 16 16' fill='currentColor'><path d='M8 1.5 9.6 6 14 7.5 9.6 9 8 13.5 6.4 9 2 7.5 6.4 6z'/>"
           "<path d='M13 11l.7 1.8 1.8.7-1.8.7L13 16l-.7-1.8-1.8-.7 1.8-.7z' opacity='.6'/></svg>")
I_TODAY = ("<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' "
           "stroke-linejoin='round'><path d='M2 11.5 6 6.5l3 2.6 5-6.1'/><path d='M10.6 3h3.4v3.4'/></svg>")

I_BOOK = ("<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' "
          "stroke-linejoin='round'><rect x='2' y='5' width='12' height='9' rx='1.5'/>"
          "<path d='M5.5 5V3.5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V5'/><path d='M2 9.2h12'/></svg>")
I_BANK = ("<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' "
          "stroke-linejoin='round'><path d='M2 6 8 2.5 14 6z'/><path d='M3.8 6.5v5.5M6.6 6.5v5.5M9.4 6.5v5.5M12.2 6.5v5.5'/>"
          "<path d='M2 14h12'/></svg>")
I_LENS = ("<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' "
          "stroke-linejoin='round'><path d='M8.5 14H3.5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v4'/>"
          "<path d='M5 5h4M5 7.5h2.5'/><circle cx='11' cy='11' r='2.2'/><path d='m12.6 12.6 1.6 1.6'/></svg>")
I_LEARN = ("<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' "
           "stroke-linejoin='round'><path d='M1.5 6 8 3l6.5 3L8 9z'/><path d='M4.5 7.6v3c0 1 1.6 2 3.5 2s3.5-1 3.5-2v-3'/>"
           "<path d='M14.5 6v3.5'/></svg>")
I_PAUSE = ("<svg class=ic viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round'>"
           "<path d='M6 4.5v7M10 4.5v7'/></svg>")

def company_display(c):
    tk = ticker_of(c)
    nm = read_positions().get(tk, {}).get("name") or c.name.rsplit("-", 1)[0].replace("_", " ")
    return tk, nm

# ---------- left navigation (2026-09-22 redesign) ----------
# David: "we don't need all the watchlist/research stuff in left hand side ... the watchlist on
# left side should just be stocks/things we own across each book and have dollar changes along
# with the percent changes relative to our position." So the sidebar is five destinations, then
# Holdings. The watchlist, the research tree, the candidate boards and the system docs moved to
# /research (research_inner / research_companies_inner / research_boards_inner below).
NAV = [("personal", "Personal book"), ("dip", "DIP Venture"), ("agent", "BrokerB agent"),
       ("research", "Research"), ("learn", "Learn")]
PERSONAL_ROUTES = ("/", "", "/today", "/recommendation", "/journal", "/brokera")


def _under(path, root):
    return path == root or path.startswith(root + "/")


def nav_key(path, view_path="", owners=None):
    """Which nav item owns a page: one answer per route, so exactly one item lights up.
    `view_path` is /view's ?path=. A ticker page belongs to the book that HOLDS the name
    (ticker_owners(); `owners` overrides it in tests), and to Research when no book does.
    The page JS carries the same table as navKey() (setActive), fed the same owners (OWN);
    tests/test_dashboard_nav.py runs both over every route and pins them together."""
    if _under(path, "/dip"):
        return "dip"
    if _under(path, "/agent"):
        return "agent"
    if _under(path, "/research") or path.startswith("/company/"):
        return "research"
    if _under(path, "/learn") or path in ("/primer", "/lookups"):
        return "learn"
    if path == "/advised":
        return "paused"
    if path == "/view":
        if view_path.startswith("_engine/agent"):
            return "agent"
        if view_path.startswith("_engine/recommendations") or "journal" in view_path.split("/")[0:2]:
            return "personal"
        return "research"          # dossiers, boards, system docs
    if path.startswith("/ticker/"):
        tk = path[len("/ticker/"):].split("/")[0].upper()
        return (ticker_owners() if owners is None else owners).get(tk, "research")
    if path in PERSONAL_ROUTES:
        return "personal"
    return ""


# ---------- holdings: what we own, every book (the sidebar list) ----------
AGENT_PF = ROOT / "_engine" / "agent" / "data" / "portfolio.json"
BOOK_NAMES = {"brokera": ("Personal book", "/"), "dip": ("DIP Venture", "/dip")}


def _rj(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def agent_positions():
    """The BrokerB agent's open positions, from its own sync snapshot (mcp_sync.py, code
    only, every 15 min in market hours)."""
    pf = _rj(AGENT_PF, {})
    return [p for p in (pf.get("positions") or []) if isinstance(p, dict)
            and p.get("symbol") and float(p.get("qty") or 0) > 0]


def held_all():
    """Every ticker owned in ANY book: BROKERA and DIP through books.py, the BrokerB agent
    through its portfolio.json. read_positions() follows the book on screen; this does not."""
    held = set()
    for slug in _books.slugs():
        pos = _rj(_books.book_dir(slug) / "positions.json", {})
        held |= {t for t, m in pos.items() if float((m or {}).get("shares") or 0) > 0}
    return held | {p["symbol"].upper() for p in agent_positions()}


BOOK_NAV = {"brokera": "personal", "dip": "dip"}


def ticker_owners():
    """{TK: nav key} for every ticker a book holds — the book a /ticker page belongs to. The
    Personal book wins a name it shares, then DIP, then the BrokerB agent (review 2026-09-22:
    ARI tapped under "BrokerB agent" in Holdings opened a page lit as the Personal book, with
    no agent position on it). A name no book holds is Research's, which nav_key() supplies."""
    own = {}
    try:
        books = _books.load()
        for slug in _books.slugs(books):
            key = BOOK_NAV.get(slug)
            if not key:
                continue
            for t, m in _rj(_books.book_dir(slug, books) / "positions.json", {}).items():
                if isinstance(m, dict) and float(m.get("shares") or 0) > 0:
                    own.setdefault(t.upper(), key)
    except Exception:
        pass
    for p in agent_positions():
        own.setdefault(p["symbol"].upper(), "agent")
    return own


def day_change(shares, price, prev):
    """Today's move ON OUR POSITION: shares × (price − previous close), and the price's own %
    move. (None, None) when a quote leg is missing — unknown is not zero. The page JS
    (holdDay) is the same arithmetic; tests pin the two together."""
    try:
        shares, price, prev = float(shares or 0), float(price or 0), float(prev or 0)
    except (TypeError, ValueError):
        return None, None
    if not shares or not price or not prev:
        return None, None
    return shares * (price - prev), (price - prev) / prev * 100


def _cached_quote(tk):
    """A quote already in memory — never a network call. The sidebar renders on every full page
    load; the page's one /api/quotes batch brings the live numbers a moment later."""
    hit = _QCACHE.get(tk)
    return (hit[1] if hit else None) or {}


def _perf_close(tk):
    hit = _CACHE.get("perf")
    return (((hit[1] if hit else None) or {}).get(tk) or {}).get("price")


def _hq(tk, shares, cost=None, snap_px=None, book="brokera"):
    """One quoted holding. Its value uses the best price already on hand — the live-quote
    cache, the book's own snapshot (the agent's sync), the perf batch's last close, else cost —
    so a row whose quote never arrives still reads as a position, never a blank."""
    q = _cached_quote(tk)
    usd, pct = day_change(shares, q.get("price"), q.get("prev"))
    px = q.get("price") or snap_px or _perf_close(tk)
    try:
        value = shares * float(px or cost or 0) or None
    except (TypeError, ValueError):
        value = None
    return {"kind": "quote", "tk": tk, "shares": shares, "book": book, "value": value,
            "at_cost": not px and bool(value), "day_usd": usd, "day_pct": pct}


def _mark_date(s):
    """'2026-06-03 · at cost' → 'Jun 3'; 'Sep 22, 3:53 PM ET' → 'Sep 22'. The year shows only
    when it is not this year, so an old mark says it is old."""
    s = str(s or "").split("·")[0].strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            d = dt.date(int(m[1]), int(m[2]), int(m[3]))
            return f"{d:%b} {d.day}" + ("" if d.year == dt.date.today().year else f", {d.year}")
        except ValueError:
            pass
    return s.split(",")[0].strip() or "—"


def holdings():
    """What we own, by book: [{slug, label, route, day_usd, rows}]. Each book is read from its
    own files — positions.json, the broker-marked note (account.json) and private.json through
    books.py; the BrokerB agent's portfolio.json — never through read_positions()."""
    groups = []
    books = _books.load()
    for slug in _books.slugs(books):
        bdir = _books.book_dir(slug, books)
        label, route = BOOK_NAMES.get(slug, ((books.get(slug) or {}).get("label") or slug, f"/{slug}"))
        rows = [_hq(tk, float(m.get("shares") or 0), m.get("cost_basis"), book=slug)
                for tk, m in _rj(bdir / "positions.json", {}).items()
                if isinstance(m, dict) and float(m.get("shares") or 0) > 0]
        rows.sort(key=lambda r: -(r["value"] or 0))
        acct = _rj(bdir / "account.json", {})
        if acct.get("structured_note"):
            rows.append({"kind": "mark", "tk": "Structured note", "value": float(acct["structured_note"]),
                         "book": slug, "route": route, "tag": "broker mark",
                         "sub": f"as of {_mark_date(acct.get('as_of'))}"})
        for x in _rj(bdir / "private.json", []):
            if isinstance(x, dict) and x.get("name"):
                rows.append({"kind": "private", "tk": x["name"], "value": float(x.get("value") or 0) or None,
                             "book": slug, "route": route, "tag": "private",
                             "sub": f"marked {_mark_date(x.get('as_of'))}"})
        if rows:
            groups.append({"slug": slug, "label": label, "route": route, "rows": rows})
    ag = sorted(agent_positions(), key=lambda p: -float(p.get("value") or 0))
    if ag:
        groups.append({"slug": "agent", "label": "BrokerB agent", "route": "/agent",
                       "rows": [_hq(p["symbol"].upper(), float(p.get("qty") or 0), p.get("avg_cost"),
                                    p.get("price"), book="agent") for p in ag]})
    for g in groups:
        known = [r["day_usd"] for r in g["rows"] if r.get("day_usd") is not None]
        g["day_usd"] = sum(known) if known else None
    return groups


def _usd_signed(v):
    return ("+" if v >= 0 else "−") + f"${abs(v):,.0f}"


def _pct_signed(v):
    """+1.3%; under 0.1% it keeps two decimals so a small move on a big position (VTI +$153 on
    $343k) reads +0.04%, not a +0.0% that contradicts its own dollar figure."""
    a = abs(v)
    return ("+" if v >= 0 else "−") + (f"{a:.2f}%" if 0 < a < 0.1 else f"{a:.1f}%")


def _hold_row(r):
    e = html.escape
    tk = e(r["tk"])
    val = f"${r['value']:,.0f}" if r.get("value") else "—"
    left = f"<span class=hl><span class=htk>{tk}</span><span class=hv>{val}</span></span>"
    if r["kind"] != "quote":   # a private or broker mark: no quote, no day change — say how it is valued
        return (f"<a class='leaf hrow priv' data-route='{r['route']}' href='{r['route']}'>{left}"
                f"<span class=hr><span class=hd>{e(r['tag'])}</span><span class=hp>{e(r['sub'])}</span></span></a>")
    d = r["day_usd"]
    cls = "" if d is None else (" up" if d >= 0 else " down")
    usd = _usd_signed(d) if d is not None else "—"
    pct = _pct_signed(r["day_pct"]) if r["day_pct"] is not None else ("at cost" if r["at_cost"] else "")
    known = f" data-usd='{d:.2f}'" if d is not None else ""
    return (f"<a class='leaf hrow' data-tk='{tk}' href='/ticker/{tk}' data-q='{tk}' data-sh='{r['shares']:g}' "
            f"data-book='{e(r['book'])}'{known}>{left}"
            f"<span class=hr><span class='hd{cls}'>{usd}</span><span class='hp{cls}'>{pct}</span></span></a>")


def holdings_html():
    # the market state rides the list (data-mkt): refreshSide() re-reads it, so the page's live
    # timers learn the bell from the same render that re-lists what we own — and the owned set
    # (data-held), so a name bought since the page loaded joins the owned tick
    try:
        mk = market_client()
    except Exception:
        mk = None
    mattr = f" data-mkt='{html.escape(json.dumps(mk))}'" if mk else ""
    try:
        groups = holdings()
    except Exception as ex:   # a bad file must never take the whole sidebar (and every page) down
        # data-mkt still rides it: without one the page re-read the sidebar every minute, all night
        return (f"<div class=hsec{mattr}><div class=hsec-h>Holdings</div><div class=sempty>Couldn't read the books: "
                f"{html.escape(type(ex).__name__)}</div></div>")
    try:
        mattr += f" data-held='{html.escape(json.dumps(sorted(held_all())))}'"
    except Exception:
        pass
    state = "" if mk is None else (" · live" if mk["open"] else " · market closed")
    s = [f"<div class=hsec{mattr}><div class=hsec-h>Holdings <span class=hint>today, on our position{state}</span></div>"]
    for g in groups:
        d = g["day_usd"]
        cls = "" if d is None else (" up" if d >= 0 else " down")
        s.append(f"<div class=hgrp><span>{html.escape(g['label'])}</span>"
                 f"<span class='hday{cls}' data-hday='{g['slug']}'>{_usd_signed(d) if d is not None else ''}</span></div>")
        s.extend(_hold_row(r) for r in g["rows"])
    if not groups:
        s.append("<div class=sempty>Nothing held in any book.</div>")
    s.append("</div>")
    return "".join(s)


def _learn_link(icon):
    link = ""
    lp = globals().get("learn_page")
    if lp is not None:
        try:
            link = lp.sidebar_link(icon)
        except Exception:
            link = ""
    if not link:   # learn_page.py not loaded: the glossary is the Learn surface that exists
        link = f"<a class='leaf navtop' data-route='/primer' href='/primer'>{icon}<span>Learn</span></a>"
    # the Library's "waiting on you" count (HBS-library pulls David owes the desk) was a badge on
    # its own sidebar link; Library now sits under Learn, so the count rides the Learn row
    try:
        n = lookups_page.open_count()
    except Exception:
        n = 0
    # "5 waiting" landed on Guides with nothing saying what waited (review 2026-09-22): the badge
    # names the thing, and the Library seg under Learn carries the same count
    if n and link.endswith("</a>"):
        link = link[:-4] + f"<span class=lheld>{n} library pull{'' if n == 1 else 's'}</span></a>"
    return link


def sidebar(route=None):
    """The left navigation. `route` = (path, view_path) of a full page render, so the right item
    is lit before any script runs; setActive() keeps it right across SPA navigation."""
    key = nav_key(*route) if route else ""

    def ni(k, link):
        return f"<div class='ni{' on' if k == key else ''}' data-nav='{k}'>{link}</div>"
    links = {
        "personal": f"<a class='leaf navtop' data-home href='/'>{I_BOOK}<span>Personal book</span></a>",
        "dip": dip_page.sidebar_link(I_BANK),
        "agent": f"<a class='leaf navtop' data-route='/agent' href='/agent'>{I_SPARK}<span>BrokerB agent</span></a>",
        "research": f"<a class='leaf navtop' data-route='/research' href='/research'>{I_LENS}<span>Research</span></a>",
        "learn": _learn_link(I_LEARN)}
    s = [f"<div class=sidetop><a class='brand' href='/' data-home>{I_LOGO}<span>Stocks</span></a></div>",
         "<form class=search onsubmit='return doSearch(event)'>" + I_SEARCH +
         "<input id=q placeholder='Search ticker or company' autocomplete=off spellcheck=false aria-label='Search'>"
         "<kbd class=skey>/</kbd><div id=sresults class=sresults></div></form>",
         "<nav class=snav>" + "".join(ni(k, links[k]) for k, _ in NAV) + "</nav>",
         holdings_html(),
         # Justin's book is a paused project: out of the list, one quiet link keeps the route
         "<div class=sfoot>" + ni("paused", f"<a class='leaf navtop' data-route='/advised' href='/advised'>"
                                           f"{I_PAUSE}<span>Paused · Justin's book</span></a>") + "</div>"]
    return "".join(s)

# ---------- financials (flags) ----------
def to_num(x):
    x = (x or "").strip().replace(",", "").replace("$", "").replace("%", "")
    if x in ("", "—", "-", "n/a", "NA"):
        return None
    try:
        return float(x)
    except ValueError:
        return None

def last(vals):
    return next((x for x in reversed(vals) if x is not None), None)

def load_model(cdir):
    mp = cdir / "financials" / "model.csv"
    if not mp.exists():
        return None, None
    rows = [r for r in csvmod.reader(mp.open()) if any(c.strip() for c in r)]
    if len(rows) < 2 or rows[0][0].strip().lower() not in ("metric", "item", ""):
        return None, None
    periods = [h.strip() for h in rows[0][1:]]
    M = {r[0].strip().lower(): [to_num(x) for x in r[1:len(rows[0])]] for r in rows[1:]}
    return periods, M

def find(M, *keys):
    for k in keys:
        for name, v in M.items():
            if k in name:
                return v
    return None

def financial_flags(cdir):
    periods, M = load_model(cdir)
    if not M:
        return ""
    hi, flags, u = [], [], "M"
    rev = find(M, "revenue")
    if rev and last(rev) is not None:
        lr = last(rev); vals = [x for x in rev if x is not None]; pk = max(vals)
        hi.append(f"Revenue ${lr:,.0f}{u} (latest); peak ${pk:,.0f}{u}")
        if pk and lr < 0.7 * pk:
            flags.append(("red", f"Revenue is {(1-lr/pk)*100:.0f}% below its ${pk:,.0f}{u} peak — a multi-year decline"))
        elif len(vals) >= 3 and vals[-1] < vals[-2] < vals[-3]:
            flags.append(("yellow", "Revenue has declined for 3+ years running"))
    nl = find(M, "net_loss", "net loss")
    if nl and last(nl) is not None and last(nl) < 0:
        flags.append(("yellow", f"Persistently unprofitable — net loss ${abs(last(nl)):,.0f}{u} latest"))
    cash = find(M, "cash_plus", "cash"); ocf = find(M, "operating_cash")
    if cash and ocf and last(cash) is not None and last(ocf) is not None and last(ocf) < 0:
        burn = abs(last(ocf)); rw = last(cash) / burn if burn else 99
        hi.append(f"Liquidity ${last(cash):,.0f}{u} vs ~${burn:,.0f}{u}/yr burn")
        if rw < 1:
            flags.append(("red", f"Only ~{rw:.1f} yr runway — acute financing risk"))
        elif rw < 2:
            flags.append(("yellow", f"~{rw:.1f} yr runway — likely raises before/at its catalyst (dilution risk)"))
        else:
            flags.append(("green", f"~{rw:.1f} yr runway — funded through next milestones"))
    liab = find(M, "total_liabilities")
    if liab is not None and cash and last(liab) is not None and last(cash) is not None and last(cash) > last(liab):
        flags.append(("green", "Net cash — liquidity exceeds total liabilities"))
    eq = find(M, "stockholders_equity", "equity"); gw = find(M, "goodwill"); it = find(M, "intangible")
    if eq and last(eq):
        soft = (last(gw) or 0) + (last(it) or 0)
        if soft > 0.5 * last(eq):
            flags.append(("yellow", f"~{soft/last(eq)*100:.0f}% of book is goodwill/intangibles — tangible equity ~${last(eq)-soft:,.0f}{u}"))
    if not (hi or flags):
        return ""
    hitml = "".join(f"<li>{html.escape(h)}</li>" for h in hi)
    flags.sort(key=lambda f: {"red": 0, "yellow": 1, "green": 2}[f[0]])
    ftml = "".join(f"<div class=flag><span class='dot {lv}'></span><span>{html.escape(t)}</span></div>" for lv, t in flags)
    return f"<div class=sec>Highlights</div><ul class=hi>{hitml}</ul><div class=sec>Flags</div><div class=flags>{ftml}</div>"

def market_header(sym, spark=True):
    sp = f"<span class=spark data-spark='{sym}'></span>" if spark else ""
    return (f"<div class=market><div class=mbody data-ticker='{sym}'>"
            f"<span class=mtk>{sym}</span><span class=mload>loading live quote…</span></div>{sp}</div>")

def render_financial(p):
    cdir = p.parent.parent
    tiles, table = kpi_and_table(p)
    return market_header(ticker_of(cdir)) + tiles + financial_flags(cdir) + table

def kpi_and_table(p):
    rows = [r for r in csvmod.reader(p.open()) if any(c.strip() for c in r)]
    if len(rows) < 2:
        return "", render_plain_csv(p)
    hdr = [h.strip() for h in rows[0]]; data = rows[1:]
    tiles = ""
    if hdr[0].lower() in ("metric", "item", ""):
        cells = []
        for r in data:
            if any(k in r[0].strip().lower() for k in KEY):
                v = last([to_num(x) for x in r[1:len(hdr)]])
                if v is None:
                    continue
                cls = "neg" if v < 0 else ""
                val = f"${v:,.0f}M" if abs(v) >= 1000 else f"${v:,.1f}M"
                cells.append(f"<div class=tile><div class=tk>{html.escape(r[0].replace('_',' '))}</div>"
                             f"<div class='tv {cls}'>{val}</div><div class=tp>{html.escape(hdr[-1])}</div></div>")
        if cells:
            tiles = f"<div class=kpis>{''.join(cells)}</div>"
    tbl = ["<div class=sec>Full series</div><div class=tablewrap><table><thead><tr>"] + \
          [f"<th>{html.escape(h)}</th>" for h in hdr] + ["</tr></thead><tbody>"]
    for r in data:
        tbl.append("<tr>")
        for ci, c in enumerate(r):
            n = to_num(c) if ci > 0 else None
            if n is None:
                tbl.append(f"<td>{html.escape(c)}</td>")
            else:
                cls = "num neg" if n < 0 else "num"
                disp = f"{n:,.0f}" if abs(n) >= 1000 else f"{n:,.2f}".rstrip("0").rstrip(".")
                tbl.append(f"<td class='{cls}'>{disp}</td>")
        tbl.append("</tr>")
    tbl.append("</tbody></table></div>")
    return tiles, "".join(tbl)

def render_plain_csv(p):
    rows = list(csvmod.reader(p.open()))
    if not rows:
        return "<p>empty</p>"
    head, *body = rows
    o = ["<div class=tablewrap><table><thead><tr>"] + [f"<th>{html.escape(c)}</th>" for c in head] + ["</tr></thead><tbody>"]
    for r in body[:1000]:
        o.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
    o.append("</tbody></table></div>")
    return "".join(o)

def render_file(p):
    """Returns (html, toc_tokens) — toc_tokens only for markdown."""
    if p.suffix.lower() == ".csv":
        body = render_financial(p) if p.parent.name == "financials" else render_plain_csv(p)
        return body, []
    md = markdown.Markdown(extensions=MD_EXTS)
    body = md.convert(p.read_text(encoding="utf-8", errors="ignore"))
    # Relative links between docs (e.g. FINAL-REPORT's "see update-<date>.md") break
    # under /view?path=… — resolve them against the doc's folder and route via the SPA.
    import re as _re
    def _fix(m):
        href = m.group(1)
        try:
            tgt = (p.parent / href).resolve().relative_to(ROOT)
        except Exception:
            return m.group(0)
        return f"href=\"/view?path={tgt}\" data-path=\"{tgt}\""
    body = _re.sub(r'href="(?!(?:[a-z][a-z0-9+.-]*:|/|#))([^"]+)"', _fix, body)
    # a markdown table is as wide as its widest row: the weekly digest's tables made /recommendation
    # 680px wide on every phone. Each one scrolls inside its own box instead of the page.
    body = _re.sub(r"<table(\s|>)", r"<div class=tablewrap><table\1", body).replace("</table>", "</table></div>")
    return body, getattr(md, "toc_tokens", [])

# ---------- thesis card (the one-page state summary; analysis/card.json) ----------
def render_card(cdir):
    f = cdir / "analysis" / "card.json"
    if not f.exists():
        return ""
    try:
        c = json.loads(f.read_text())
    except Exception:
        return ""
    STATE = {"working": ("buy", "Thesis working"), "strengthening": ("buy", "Thesis strengthening"),
             "intact": ("neu", "Thesis intact"), "stressed": ("warn", "Thesis stressed"),
             "broken": ("sell", "Thesis broken")}
    scls, slab = STATE.get((c.get("state") or "").lower(), ("neu", c.get("state", "—")))
    out = [f"<div class=tcard><div class=tchead><span class='pill {scls}'>{html.escape(slab)}</span>"
           f"<span class=muted>as of {html.escape(c.get('as_of',''))}</span></div>"]
    if c.get("thesis"):
        out.append(f"<div class=tcthesis>{html.escape(c['thesis'])}</div>")
    if c.get("state_note"):
        out.append(f"<div class=tcnote>{html.escape(c['state_note'])}</div>")
    cols = []
    for key, lab in (("now", "Now"), ("later", "Later")):
        items = c.get(key) or []
        if items:
            lis = "".join(f"<li>{html.escape(x)}</li>" for x in items)
            cols.append(f"<div class=tccol><div class=tclab>{lab}</div><ul>{lis}</ul></div>")
    if c.get("kill"):
        lis = "".join(f"<li>{html.escape(x)}</li>" for x in c["kill"])
        cols.append(f"<div class=tccol><div class='tclab kill'>What kills it</div><ul>{lis}</ul></div>")
    if cols:
        out.append(f"<div class=tccols>{''.join(cols)}</div>")
    if c.get("ladder"):
        rungs = "".join(f"<div class=tcrung><span class=tcz>{html.escape(str(r.get('zone','')))}</span>"
                        f"<span class=tca>{html.escape(str(r.get('action','')))}</span>"
                        f"<span class='tcs{' live' if 'LIVE' in str(r.get('status','')).upper() else ''}'>"
                        f"{html.escape(str(r.get('status','')))}</span></div>" for r in c["ladder"])
        # render-time truth: declared working orders (config/open_orders.json) overlay the
        # card the moment David journals a placement — no session refresh needed
        tk_card = ticker_of(cdir)
        for o in _json("open_orders.json", []):
            if o.get("tk") == tk_card:
                rungs += (f"<div class=tcrung><span class=tcz>${o.get('limit'):,.2f}</span>"
                          f"<span class=tca>{html.escape(o.get('side',''))} {o.get('qty'):,} (GTC)</span>"
                          f"<span class='tcs live'>WORKING — declared {html.escape(o.get('placed',''))}</span></div>")
        out.append(f"<div class=tclab style='margin-top:12px'>Ladder</div><div class=tcladder>{rungs}</div>")
    if c.get("milestones"):
        ms = "".join(f"<span class=tcms><b>{html.escape(str(m.get('date','')))}</b> {html.escape(str(m.get('label','')))}</span>"
                     for m in sorted(c["milestones"], key=lambda x: str(x.get("date", ""))))
        out.append(f"<div class=tcmsrow>{ms}</div>")
    out.append("</div>")
    return "".join(out)

# ---------- investor lenses ----------
def sig_cls(s):
    # build-064: THE single implementation — jpm_page delegates here. Order matters:
    # "Speculative Buy" must read as caution (amber), not buy, so the warn set wins first;
    # "Pass" is a bearish verdict and colors accordingly.
    s = (s or "").strip().lower()
    if "not a buy" in s:          # the substring hazard the 2026-08-27 hunt recorded
        return "sell"
    if s.startswith("buy below") or s.startswith("follow"):
        # v0.3 scale (EVALUATION-FRAMEWORK §Verdict): actionable at a price, not yet — amber
        return "warn"
    if any(k in s for k in ("caution", "warn", "mixed", "risk", "speculative")):
        return "warn"
    if any(k in s for k in ("bear", "sell", "negative", "avoid", "pass")):
        return "sell"
    if any(k in s for k in ("bull", "buy", "positive", "accumulate")):
        return "buy"
    return "neu"

def lenses_overall(cdir):
    """The verdict line ('Buy below $36.15'). The price in it is the report's own buy_below
    number, not the rounded one the signal text may carry: VSNT's signal said $36 while its
    buy_below, the Brief and the teardown guide said $36.15 (review 2026-09-22)."""
    f = cdir / "analysis" / "lenses.json"
    try:
        o = json.loads(f.read_text()).get("overall", {}) if f.exists() else {}
    except Exception:
        return ""
    sig = str(o.get("signal") or "")
    bb = o.get("buy_below")
    if isinstance(bb, (int, float)) and not isinstance(bb, bool) and bb > 0 and "$" in sig:
        px = f"${bb:,.0f}" if abs(bb - round(bb)) < 0.005 else f"${bb:,.2f}"
        sig = re.sub(r"\$[\d,]+(?:\.\d+)?", lambda _m: px, sig, count=1)
    return sig

def render_lenses(cdir):
    f = cdir / "analysis" / "lenses.json"
    if not f.exists():
        return ""
    try:
        data = json.loads(f.read_text())
    except Exception:
        return ""
    o = data.get("overall", {})
    out = ["<div class=lenswrap>"]
    if o:
        # v0.3 verdict fields (EVALUATION-FRAMEWORK §Verdict): the price that changes the answer
        def _m(v):
            try:
                return f"${float(v):,.2f}"
            except Exception:
                return "—"
        vline = ""
        if o.get("buy_below") is not None or o.get("base") is not None:
            parts = []
            if o.get("bar"):
                parts.append(f"{html.escape(str(o['bar']))} bar")
            if o.get("buy_below") is not None:
                parts.append(f"buy below <b>{_m(o['buy_below'])}</b>")
            if o.get("base") is not None:
                parts.append(f"bear {_m(o.get('bear'))} · base {_m(o.get('base'))} · bull {_m(o.get('bull'))}")
            if o.get("price") is not None:
                parts.append(f"struck at {_m(o['price'])}{' on ' + html.escape(str(o['as_of'])) if o.get('as_of') else ''}")
            vline = f"<div class=vbar>{' · '.join(parts)}</div>"
        out.append(f"<div class='verdict {sig_cls(o.get('signal'))}'>"
                   f"<div class=vlab>Overall read</div>"
                   f"<div class=vsig>{html.escape(o.get('signal',''))}"
                   f"<span class=vconf>{html.escape(o.get('confidence',''))} confidence</span></div>"
                   f"{vline}"
                   f"<div class=vsum>{html.escape(o.get('summary',''))}</div></div>")
    out.append("<div class=lensgrid>")
    for l in data.get("lenses", []):
        c = sig_cls(l.get("signal"))
        conf = (l.get("confidence") or "").strip()
        pct = {"high": 100, "medium": 62, "med": 62, "low": 32}.get(conf.lower(), 50)
        out.append(f"<div class=lens><div class=lhead><span class=lname>{html.escape(l.get('lens',''))}</span>"
                   f"<span class='pill {c}'>{html.escape(l.get('signal',''))}</span></div>"
                   f"<div class=lconf>{html.escape(conf)} confidence</div>"
                   f"<div class='cbar {c}'><span style='width:{pct}%'></span></div>"
                   f"<div class=lnote>{html.escape(l.get('note',''))}</div></div>")
    out.append("</div></div>")
    return "".join(out)

# ---------- routes ----------
BOOT = str(int(time.time()))  # server build id — stale SPA tabs self-reload when this changes

@app.after_request
def _ver_header(resp):
    resp.headers["X-App-Ver"] = BOOT
    return resp

# Network guard: loopback + Tailscale only. The server binds 0.0.0.0, but endpoints
# here can launch agent TRADE sessions — plain-LAN clients (192.168.x) get 403.
import ipaddress
_ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "100.64.0.0/10", "::1/128")]

@app.before_request
def _net_guard():
    try:
        ip = ipaddress.ip_address((request.remote_addr or "").split("%")[0])
    except ValueError:
        abort(403)
    if not any(ip in n for n in _ALLOWED_NETS):
        abort(403)
    # Mutations are POST-only and same-origin (REVIEW-PLAN §1E, 2026-09-12). Twelve routes used
    # to launch Claude sessions or move directories on a GET — one <img src> on any tailnet page
    # could fire them. A browser sends Origin / Sec-Fetch-Site on every cross-site POST, so a
    # mismatch is refused; our own fetch() helper (`post()` in the JS) is same-origin by
    # construction. curl from the box passes (no Origin, no Sec-Fetch-Site) — that is the CLI.
    if request.method == "POST" and request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin and origin.split("//", 1)[-1].rstrip("/") != request.host:
            abort(403)
        sfs = request.headers.get("Sec-Fetch-Site")
        if sfs and sfs not in ("same-origin", "none"):
            abort(403)

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E"
           "%3Crect width='20' height='20' rx='4' fill='%232563eb'/%3E"
           "%3Crect x='3.5' y='11' width='3' height='6' rx='1' fill='white'/%3E"
           "%3Crect x='8.5' y='7.5' width='3' height='9.5' rx='1' fill='white'/%3E"
           "%3Crect x='13.5' y='3' width='3' height='14' rx='1' fill='white'/%3E%3C/svg%3E")

# Seg bars (David sign-off 2026-08-13): each section of a book is its own route, shown as a
# sticky bar of segs; the bar rides the global [data-route] interception, and wrap() injects it
# BEFORE the partial return so SPA swaps carry it too. 2026-09-22 redesign: the book is the
# "Personal book", its brief (/brokera, was "David brief") joins the bar, and Research left it to
# become a top-level page with its own bar (Watchlist · Companies · Boards).
PF_SEGS = [("/", "Book"), ("/brokera", "Brief"), ("/today", "Today"),
           ("/recommendation", "Advice"), ("/journal", "Journal")]
RS_SEGS = [("/research", "Watchlist"), ("/research/companies", "Companies"),
           ("/research/boards", "Boards")]


def _segbar(segs, active, back=False):
    b = ""
    if back:   # standard detail-page affordance: back chevron (history-aware) + the lit section
        home = active or segs[0][0]
        b = ("<a class='pfseg pfback' href='#' onclick='history.length>1?history.back():"
             f"nav(\"{home}\",true);return false' title='Back' aria-label='Back'>‹</a>")
    return ("<div class=pfnav>" + b + "".join(
        f"<a class='pfseg{' on' if active == p else ''}' data-route='{p}' href='{p}'>{lab}</a>"
        for p, lab in segs) + "</div>")


_BACK = ("<a class='pfseg pfback' href='#' onclick='history.length>1?history.back():"
         "nav(\"{home}\",true);return false' title='Back' aria-label='Back'>‹</a>")


def _agent_backbar():
    """The BrokerB agent has its own tabs inside /agent: its documents and the names it holds
    get a back chevron and one link home."""
    return ("<div class=pfnav>" + _BACK.format(home="/agent")
            + "<a class=pfseg data-route='/agent' href='/agent'>BrokerB agent</a></div>")


def pfseg():
    """L2 nav (sections of a book) — and L3 INHERITANCE (David, 2026-08-13): a detail page keeps
    the bar of the place it belongs to, with a back chevron, so clicking around never strands
    you. The owner matches nav_key(): ticker pages → the Personal book bar; dossiers, boards and
    system docs → Research; agent documents → a back-bar to /agent (that book has its own tabs)."""
    from flask import request as _rq
    path = _rq.path
    if _under(path, "/dip"):   # DIP Venture: Book · Options · Industries · PE · Judgements
        return dip_page.pfseg(path)
    lp = globals().get("learn_page")
    if lp is not None and (_under(path, "/learn") or path in ("/primer", "/lookups")):
        try:
            return lp.seg(path)   # Learn: Guides · Glossary · Library
        except Exception:
            return ""
    if path in {p for p, _ in PF_SEGS}:
        return _segbar(PF_SEGS, path)
    if path in {p for p, _ in RS_SEGS}:
        return _segbar(RS_SEGS, path)
    if path.startswith("/ticker/"):
        # the bar of the book that holds the name (nav_key): Personal → its bar; the agent → a
        # back-bar to /agent; DIP → its bar; held nowhere → Research's
        own = nav_key(path)
        if own == "agent":
            return _agent_backbar()
        if own == "dip":
            return dip_page.pfseg(path).replace("<div class=pfnav>", "<div class=pfnav>" + _BACK.format(home="/dip"), 1)
        if own == "research":
            return _segbar(RS_SEGS, None, back=True)
        return _segbar(PF_SEGS, None, back=True)
    if path.startswith("/company/"):
        return _segbar(RS_SEGS, "/research/companies", back=True)
    if path == "/view":
        vp = _rq.args.get("path", "")
        if vp.startswith("_engine/agent"):
            return _agent_backbar()
        if vp.startswith("_engine/recommendations"):
            return _segbar(PF_SEGS, "/recommendation", back=True)
        if "journal" in vp.split("/")[0:2]:
            return _segbar(PF_SEGS, "/journal", back=True)
        if vp.startswith("_engine"):   # candidate boards and the system docs
            return _segbar(RS_SEGS, "/research/boards", back=True)
        return _segbar(RS_SEGS, "/research/companies", back=True)   # a company's dossier
    return ""


def wrap(title, inner, partial):
    inner = pfseg() + inner
    if partial:
        return inner
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<meta name=color-scheme content='light dark'>
<meta name=theme-color content='#ffffff' media='(prefers-color-scheme: light)'>
<meta name=theme-color content='#15181d' media='(prefers-color-scheme: dark)'>
<title>{html.escape(title)} · Stocks</title>
<link rel=icon href="{FAVICON}">
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel=stylesheet>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>{CSS}</style></head><body>
<script>var WATCH={json.dumps(watchlist())},HELD={json.dumps(sorted(held_all()))},OWN={json.dumps(ticker_owners(), sort_keys=True)},APPV='{BOOT}',SYNCED={read_account().get("as_of_epoch", 0)},MKT={json.dumps(market_client())};</script>
<input type=checkbox id=nav hidden>
<div class=mobilebar><label for=nav class=burger aria-label=Menu><svg viewBox='0 0 16 16' width=18 height=18 fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'><path d='M2 4h12M2 8h12M2 12h12'/></svg></label>
<a class='brand mbrand' href='/' data-home>{I_LOGO}<span>Stocks</span></a></div>
<label for=nav class=scrim></label>
<aside class=side>{sidebar((request.path, request.args.get('path', '')))}</aside>
<main id=main>{inner}</main><script>{JS}</script></body></html>"""

def pos_row(tk, meta, holding=False):
    cf = company_folder(tk)
    ov = lenses_overall(cf) if cf else ""
    verdict = f"<span class='pill {sig_cls(ov)}'>{html.escape(ov)}</span>" if ov else "<span class=muted>—</span>"
    name = html.escape(meta.get("name") or (cf.name if cf else tk))
    cat = html.escape(meta.get("next_catalyst") or "—")
    sh = (meta.get("shares") or 0) if holding else 0  # only holdings count toward KPI (avoid double-count in watchlist)
    cost = (meta.get("cost_basis") or 0) if holding else 0
    val = "<td class='num val'>·</td>" if holding else ""
    return (f"<tr class=posrow data-tk='{tk}' data-shares='{sh}' data-cost='{cost}'>"
            f"<td><a class=tklink data-tk='{tk}' href='/ticker/{tk}'>{tk}</a></td>"
            f"<td>{name}</td><td class='num price'>·</td><td class='num chg'></td>{val}"
            f"<td>{verdict}</td><td class=cat>{cat}</td>"
            f"<td class=rmc><button class=rm title='remove' onclick='removeWatch(event,\"{tk}\")'>×</button></td></tr>")

def hold_row(tk, meta):
    cf = company_folder(tk); ov = lenses_overall(cf) if cf else ""
    verdict = f"<span class='pill {sig_cls(ov)}'>{html.escape(ov)}</span>" if ov else "<span class=muted>—</span>"
    name = html.escape(meta.get("name") or tk)
    sh = meta.get("shares") or 0; avg = meta.get("cost_basis") or 0
    # the Book is where a held name should answer everything: what it cost, what it's
    # worth, how it has traded, and where the write-up is (David 2026-08-13)
    doc = "<span class=muted>—</span>"
    if cf:
        for pref in ("analysis/FINAL-REPORT.md", "analysis/soft-research-dossier.md"):
            if (cf / pref).exists():
                rel = html.escape(str((cf / pref).relative_to(ROOT)))
                doc = f"<a class=doclink data-path='{rel}' href='/view?path={rel}'>Dossier</a>"
                break
    return (f"<tr class='posrow nrow' data-tk='{tk}' data-shares='{sh}' data-cost='{avg}'>"
            f"<td><a class=tklink data-tk='{tk}' href='/ticker/{tk}'>{tk}</a><div class=subname>{name}</div></td>"
            f"<td class=num>{sh:,.0f}</td><td class=num>${avg:,.2f}</td><td class='num price'>·</td>"
            f"<td class='num mval'>·</td><td class='num pnl'>·</td><td class='num chg'></td>"
            f"<td class='num p-m6 c-wide'></td><td class='num p-ytd c-wide'></td>"
            f"<td class='num wt'>·</td><td>{verdict}</td><td class=c-wide>{doc}</td></tr>")

def home_inner():
    """The book page. Written for the BROKERA book; since 2026-09-11 it renders whichever
    book _book() names (David: "I want dip to have the same home page as jp morgan").
    Book-specific bits are marked `bk ==` / `is_jpm` — everything else is the same
    grammar, because the capital in every human-traded book asks the same questions."""
    bk = _book(); is_jpm = bk == "brokera"
    blabel = _books.load()[bk]["label"]
    if is_jpm:            # books.json still calls it "J.P. Morgan"; the page and the nav say Personal book
        blabel = "Personal book"
    bhome = "/" if is_jpm else f"/{bk}"
    pos = read_positions(); wl = watchlist(); acct = read_account()
    acct_ok = bool(acct)  # account.json missing/unreadable renders as {} — don't let $0 masquerade as a real balance
    cash = acct.get("cash", 0); mmf = acct.get("money_market", 0); yld = acct.get("mmf_yield", 0.043)
    held = [tk for tk, m in pos.items() if (m.get("shares") or 0) > 0]
    watch = [t for t in dict.fromkeys(list(wl) + list(pos.keys())) if t not in held]  # names you track but don't own

    asof = str(acct.get("as_of", "")) or "never"
    # 2026-09-22: the BROKERA book reads as "Personal book" (David); the watchlist's Add box moved to
    # /research with the watchlist itself
    head = (f"<div class=pagehead><div><h1>{'Personal book' if is_jpm else html.escape(blabel)}</h1>"
            f"<p class=muted>Live quotes · brokerage synced <b>{html.escape(asof)}</b> <span id=syncnote class=hint></span></p></div>"
            "<div class=headactions>"
            + f"<button class=connectbtn onclick='syncBrokerage()'>{I_REFRESH}<span>Sync {html.escape(blabel)}</span></button></div></div>"
            # book context for the page JS: every /api/ fetch carries book=<slug> while this is on screen
            + f"<span id=bookctx data-book='{bk}' data-home='{bhome}' hidden></span>")
    kpi = ("<div class=kpirow>"
           "<div class='kpi hero'><div class=kk>Account value <span class=ksub>live quotes</span></div><div class=kv id=kv-val>—</div></div>"
           "<div class=kpi><div class=kk>Today</div><div class=kv id=kv-day>—</div></div>"
           "<div class=kpi><div class=kk>Unrealized P&amp;L</div><div class=kv id=kv-ret>—</div></div>"
           "<div class=kpi><div class=kk>Lifetime P&amp;L <span class='hint' id=kv-lifesub></span></div><div class=kv id=kv-life>—</div></div>"
           "<div class=kpi><div class=kk>Cash available</div><div class=kv id=kv-cash>—</div></div></div>")
    note_v = acct.get("structured_note", 0)
    note_lbl = acct.get("structured_note_label", "Structured note")
    priv = read_external(); priv_v = sum(float(x.get("value") or 0) for x in priv) if not is_jpm else 0.0
    # data-private: hand-marked private stakes count toward a book's value (DIP: Waffle).
    # The BROKERA book keeps its external.json OUT of account value, as it always has (0 here).
    acctdata = f"<span id=acct data-cash='{cash}' data-mmf='{mmf}' data-note='{note_v}' data-private='{priv_v}' data-yield='{yld}' hidden></span>"

    if held:
        rows = "".join(hold_row(t, pos.get(t, {})) for t in held)
        hold_w = ("<div class=widget><div class=whead><span class=wtitle>Holdings</span></div><div class=wbody>"
                  "<table class=dt id=holdtable><thead><tr><th>Position</th><th class=num>Shares</th><th class=num>Avg cost</th>"
                  "<th class=num>Price</th><th class=num>Mkt value</th><th class=num>Unreal P&amp;L</th><th class=num>Day</th>"
                  "<th class='num c-wide'>6M</th><th class='num c-wide'>YTD</th>"
                  f"<th class=num>Weight</th><th>Verdict</th><th class=c-wide>Write-up</th></tr></thead>"
                  f"<tbody>{rows}</tbody></table></div></div>")
    else:
        hold_w = ("<div class=widget><div class=whead><span class=wtitle>Holdings</span></div>"
                  + ("<div class=pfempty2>No holdings tracked yet — use Sync Personal book (top right).</div></div>" if is_jpm else
                     "<div class=pfempty2>No public positions yet — the cash is the position until a name earns it.</div></div>"))
    if not is_jpm:
        hold_w += dip_page.private_widget(priv)   # the book's hand-marked private stakes (Waffle)
        hold_w += dip_page.options_widget()       # the advisor's options, evaluated (config/dip/options.json)
        hold_w = dip_page.connect_widget(bk) + hold_w

    wrows = "".join(pos_row(t, pos.get(t, {}), False) for t in watch)
    watch_w = (f"<div class=widget><div class=whead><span class=wtitle>Watchlist</span><span class=wcount>{len(watch)}</span></div><div class=wbody>"
               "<table class=dt><thead><tr><th>Ticker</th><th>Name</th><th class=num>Price</th><th class=num>Day</th>"
               f"<th>Verdict</th><th>Next catalyst</th><th></th></tr></thead><tbody>{wrows}</tbody></table></div></div>") if watch else \
              "<div class=widget><div class=whead><span class=wtitle>Watchlist</span></div><div class=pfempty2>Empty — add a ticker or use a candidate board.</div></div>"

    inc = mmf * yld
    # money market = itemized stable-NAV funds + an un-itemized broker sweep (the residual).
    # Name the funds, and only mention the plug when it's big enough to matter.
    mmf_tks = acct.get("mmf_holdings") or []
    mmf_res = float(acct.get("mmf_residual") or 0)
    mmf_bits = [", ".join(mmf_tks)] if mmf_tks else []
    if mmf_res >= max(500.0, 0.005 * (acct.get("total_value") or 0)):
        mmf_bits.append(f"${mmf_res:,.0f} broker sweep")
    mmf_sub = f" <span class=hint>{html.escape(' + '.join(mmf_bits))}</span>" if mmf_bits else ""
    acct_w = ("<div class=widget><div class=whead><span class=wtitle>Account</span>"
              f"<span class=wcount>as of {html.escape(str(acct.get('as_of','')))}</span></div><div class='wbody pad'>"
              "<div class=arow><span>Stocks &amp; ETFs</span><span id=ac-stocks class=amono>—</span></div>"
              f"<div class=arow><span>Cash</span><span class=amono>{f'${cash:,.0f}' if acct_ok else '—'}</span></div>"
              f"<div class=arow><span>Money market{mmf_sub}</span><span class=amono>{f'${mmf:,.0f}' if acct_ok else '—'}</span></div>"
              + (f"<div class=arow><span title='{html.escape(note_lbl)}'>Structured note <span class=hint>2/2028</span></span>"
                 f"<span class=amono>${note_v:,.0f}</span></div>" if note_v else "")
              + (f"<div class=arow><span>Private holdings <span class=hint>marked by hand</span></span><span class=amono>${priv_v:,.0f}</span></div>" if priv_v else "")
              + f"<div class='arow total'><span>Total account</span><span id=ac-total class=amono>—</span></div>"
              f"<div class=arow><span>Est. income · {yld*100:.1f}%</span><span class=amono>{f'~${inc:,.0f}/yr' if acct_ok else '—'}</span></div>"
              f"<div class=arow><span>Dry powder <span class=hint>cash + money market</span></span>"
              f"<span class=amono>{f'${cash + mmf:,.0f}' if acct_ok else '—'}</span></div></div></div>")
    pfranges = "".join(f"<button class='rbtn{' on' if r == '3M' else ''}' data-pfrange='{r}'>{lbl}</button>"
                       for r, lbl in [("1W", "1W"), ("1M", "1M"), ("3M", "3M"), ("6M", "6M"),
                                      ("YTD", "YTD"), ("ALL", "All")])
    # three different questions, so say which one each answers (David 2026-08-13:
    # "little confused on performance vs value vs holding anymore")
    pfmodes = ("<div class=ranges id=pfmode>"
               "<button class='rbtn on' data-pfmode='val' title='What the account is worth — stocks &amp; ETFs "
               "+ money market + cash + the structured note. Deposits push this up.'>Total value</button>"
               "<button class=rbtn data-pfmode='perf' title='What you actually made — total value with deposits, "
               "withdrawals and transfers removed. Dashed line is SPY given the same capital on the same dates.'>Return</button>"
               "<button class=rbtn data-pfmode='hold' title='Unrealized P&amp;L on the equity book alone — cost basis "
               "vs daily closes. Ignores cash, money market and the note.'>Stock P&amp;L</button></div>")
    pf_w = ("<div class=widget><div class='whead wrapx'><span class=wtitle>Portfolio</span>"
            f"<div style='display:flex;gap:14px;flex-wrap:wrap;align-items:center'>{pfmodes}<div class=ranges>{pfranges}</div></div></div>"
            "<div class='wbody pad'><div id=pfsum class=pfsum2></div>"
            "<div class=chartbox style='height:300px'><canvas id=pfchart></canvas></div></div></div>")
    skel = "".join("<div class=skelrow><span class='skel w1'></span><span class='skel w2'></span><span class='skel w3'></span></div>" for _ in range(3))
    tx_w = ("<div class=widget><div class=whead><span class=wtitle>Transactions</span></div>"
            "<div class=wbody><table class=dt id=txtable><thead><tr><th>Date</th><th>Activity</th><th>Company</th>"
            "<th class=num>Units</th><th class=num>Price</th><th class=num>Amount</th></tr></thead>"
            f"<tbody><tr><td colspan=6>{skel}</td></tr></tbody></table></div></div>")
    alloc_w = ("<div class=widget><div class=whead><span class=wtitle>Allocation</span></div>"
               "<div class='wbody pad'><div class=chartbox style='height:200px'><canvas id=allocchart></canvas></div>"
               "<div id=alloclegend class=alloclegend></div></div></div>")
    # portfolio alerts: trigger-engine signals that hit the BROKERA book (agent-book ones live on /agent)
    try:
        _al = json.loads((ROOT / "_engine" / "agent" / "data" / "alerts.json").read_text())
    except Exception:
        _al = []
    pal_raw = [a for a in _al if "BROKERA" in (a.get("book") or "")
               or not ("Agent" in (a.get("book") or "") or a.get("symbol") == "AGENT")] if is_jpm else []
    # (the trigger engine reads the BROKERA book only — a DIP book with no public names has nothing to alert on yet)
    _seen = {}
    for a in sorted(pal_raw, key=lambda x: str(x.get("ts", ""))):
        _seen[(a.get("symbol"), a.get("kind"))] = a  # latest per (symbol, kind)
    pal = sorted(_seen.values(), key=lambda x: str(x.get("ts", "")), reverse=True)[:8]
    alerts_w = ""
    if pal:
        arows = "".join(
            f"<div class=fitem><div class=fhead><span class=fdate>{html.escape(str(a.get('ts',''))[5:16].replace('T',' '))}</span>"
            f"<a class=ftk data-tk='{html.escape(a.get('symbol') or '')}' href='/ticker/{html.escape(a.get('symbol') or '')}'>{html.escape(a.get('symbol') or '')}</a>"
            f"<span class='badge{' mat' if a.get('kind') in ('price level','earnings') or a.get('kind')=='filing' else ''}'>{html.escape(a.get('kind',''))}</span>"
            f"<span class=fdesc>{html.escape(a.get('msg',''))}</span></div></div>" for a in pal)
        alerts_w = ("<div class=widget><div class=whead><span class=wtitle>Portfolio alerts "
                    "<span class=hint>— trigger engine, held names</span></span>"
                    f"<span class=wcount>{len(pal)}</span></div><div class=feed>" + arows + "</div></div>")
    # Book = portfolio only (David 2026-08-13): chart+holdings+transactions main,
    # alerts+allocation+account rail. The watchlist, calendar and what's-new live on
    # /research (their loaders find the ids there).
    rail = alerts_w + alloc_w + acct_w
    return (head + acctdata + kpi + "<div class=homegrid><div class=homemain>" + pf_w + hold_w + tx_w +
            "</div><div class=homerail>" + rail + "</div></div>")

def _walk_toc(toks, out):
    for t in toks:
        out.append(t)
        _walk_toc(t.get("children", []), out)

def view_inner(rel):
    p = safe(rel)
    if not p.exists() or p.is_dir():
        abort(404)
    parts = p.relative_to(ROOT).parts
    crumb = ["<a data-home href='/'>Portfolio</a>"]
    top = ROOT / parts[0]
    if len(parts) > 1 and top.is_dir() and not parts[0].startswith(("_", ".")):
        tk, nm = company_display(top)
        crumb.append(f"<a data-tk='{tk}' href='/ticker/{tk}'>{html.escape(nm)} · {tk}</a>")
        crumb += [html.escape(x) for x in parts[1:-1]]
    else:
        crumb += [html.escape(x) for x in parts[:-1]]
    body, toc = render_file(p)
    head = f"<div class=crumb>{'<span class=csep>/</span>'.join(crumb)}</div><h1 class=doctitle>{html.escape(label(p))}</h1>"
    return head + article_with_toc(body, toc)

def article_with_toc(body, toc):
    flat = []; _walk_toc(toc, flat)
    h2s = [t for t in flat if t["level"] == 2]
    if len(h2s) >= 3:
        items = []
        for t in flat:
            if t["level"] == 2:
                items.append(f"<a class=tocl href='#{t['id']}'>{html.escape(t['name'])}</a>")
            elif t["level"] == 3:
                items.append(f"<a class='tocl sub' href='#{t['id']}'>{html.escape(t['name'])}</a>")
        toc_html = f"<aside class=doctoc><div class=tochead>On this page</div>{''.join(items)}</aside>"
        return f"<div class=docgrid><article>{body}</article>{toc_html}</div>"
    return f"<article>{body}</article>"

def recs_all():
    rd = ROOT / "_engine" / "recommendations"
    return sorted(rd.glob("rec_*.md"), reverse=True) if rd.exists() else []

def rec_inner():
    recs = recs_all()
    today = dt.date.today().isoformat()
    have_today = any(r.stem == f"rec_{today}" for r in recs)
    btn = "" if have_today else "<button class='btn primary' onclick='genRec()'>✦ Generate new digest</button>"
    head = ("<div class=pagehead><div><h1>Advice</h1>"
            "<p class=muted>A strategist's weekly digest on the portfolio against your goals — "
            "generated on the Spark by a headless Claude session (Mondays 8:00 ET, or on demand).</p></div>"
            f"<div class=headactions>{btn}</div></div>")
    try:
        fb = json.loads(FEEDBACK.read_text()) if FEEDBACK.exists() else []
    except Exception:
        fb = []
    fbrows = "".join(f"<div class=fbitem><span class=fbdate>{html.escape(e.get('date',''))}</span>"
                     f"<span class=fbmsg>{html.escape(e.get('msg',''))}</span>"
                     f"<button class=rm title='withdraw' onclick='fbDel({e.get('id',0)})'>×</button></div>" for e in fb)
    fb_w = ("<div class=widget style='margin-bottom:22px'><div class=whead><span class=wtitle>Your standing feedback"
            "<span class=hint> — read by every future digest</span></span>"
            f"<span class=wcount>{len(fb)}</span></div><div class='wbody pad'>"
            + (f"<div class=fblist>{fbrows}</div>" if fb else
               "<p class='muted' style='margin:0 0 10px'>Talk to Claude indirectly: e.g. “I don't want the $75k VTI buy — stop recommending it” "
               "or “focus ideas on industrials”. Each note is injected into every future weekly digest until you withdraw it.</p>")
            + "<form class=fbform onsubmit='return fbSend(event)'>"
              "<textarea id=fbmsg rows=2 placeholder='Tell the next digest something — a decline, a preference, a question…'></textarea>"
              "<button class='btn primary' type=submit>Save note</button></form></div></div>")
    out = [head, "<div id=rec-status class=rstatus style='display:none'></div>", fb_w]
    if not recs:
        out.append("<div class=widget><div class=pfempty2>No recommendations yet. Claude reads your investor profile, "
                   "positions, watchlist, candidate boards, and every dossier — then writes the day's read: composition, "
                   "concrete moves, ideas worth research, and the bear case on its own advice.</div></div>")
        return "".join(out)
    latest = recs[0]
    d = latest.stem.replace("rec_", "")
    out.append(f"<div class=recdate><span class='badge mat'>Latest</span><span class=muted>{html.escape(d)}</span></div>")
    body, toc = render_file(latest)
    out.append(article_with_toc(body, toc))
    if len(recs) > 1:
        out.append("<div class=sec>Previous</div><div class=chips>")
        for r in recs[1:31]:
            rel = r.relative_to(ROOT)
            out.append(f"<a class=chip data-path='{rel}' href='/view?path={rel}'>{r.stem.replace('rec_','')}</a>")
        out.append("</div>")
    return "".join(out)

def journal_inner():
    notes = _jfile("notes.json"); decs = _jfile("decisions.json")
    # ticker combo: dropdown of every name you track (typo-proof) + free typing for new ones
    pos = read_positions()
    known = list(dict.fromkeys([t for t, m in pos.items() if (m.get("shares") or 0) > 0]
                               + watchlist() + [ticker_of(c) for c in companies()]))
    tklist = "<datalist id=tklist>" + "".join(f"<option value='{t}'>" for t in known) + "</datalist>"
    head = ("<div class=pagehead><div><h1>Journal</h1>"
            "<p class=muted>Your dated thinking — quick notes and a decision log with thesis, expectation, and "
            "kill-switch per trade. Claude reads all of it for context (the feedback box on /recommendation "
            "<i>steers</i>; this <i>records</i> — and it's what your process gets scored on).</p></div></div>")
    nrows = "".join(
        f"<div class=fbitem><span class=fbdate>{html.escape(e.get('date',''))}</span>"
        + (f"<a class=ftk data-tk='{html.escape(e['tk'])}' href='/ticker/{html.escape(e['tk'])}'>{html.escape(e['tk'])}</a>" if e.get("tk") else "")
        + f"<span class=fbmsg>{html.escape(e.get('msg',''))}</span>"
        f"<button class=rm title='delete' onclick='jnDel({e.get('id',0)})'>×</button></div>"
        for e in sorted(notes, key=lambda x: (x.get("date",""), x.get("id",0)), reverse=True))
    note_w = ("<div class=widget style='margin-bottom:22px'><div class=whead><span class=wtitle>Notes</span>"
              f"<span class=wcount>{len(notes)}</span></div><div class='wbody pad'>"
              + tklist +
              "<form class=fbform onsubmit='return jnAdd(event)'>"
              "<input id=jn-tk class=jtk list=tklist placeholder='TKR' maxlength=8 autocapitalize=characters>"
              "<textarea id=jn-msg rows=2 placeholder='A dated thought — market read, position feeling, lesson, question to future-you…'></textarea>"
              "<button class='btn primary' type=submit>Add note</button></form>"
              + (f"<div class=fblist style='margin-top:14px'>{nrows}</div>" if notes else "") + "</div></div>")
    def _dcard(e):
        open_ = e.get("status") != "closed"
        meta = " · ".join(x for x in [e.get("action","").upper(),
                                      f"{e.get('qty')} @ ${e.get('price')}" if e.get("qty") else "",
                                      f"src: {e.get('source')}" if e.get("source") else ""] if x)
        closebtn = (f"<button class='wbtn' onclick='jdClose({e.get('id',0)})'>Close out</button>" if open_
                    else f"<span class='pill neu'>closed {html.escape(e.get('closed',''))} · score {e.get('process_score','—')}/5</span>")
        outcome = f"<div class=jrow><b>Outcome</b>{html.escape(e.get('outcome',''))}</div>" if e.get("outcome") else ""
        return (f"<div class=jcard><div class=jhead><span class=fbdate>{html.escape(e.get('date',''))}</span>"
                + (f"<a class=ftk data-tk='{html.escape(e['tk'])}' href='/ticker/{html.escape(e['tk'])}'>{html.escape(e['tk'])}</a>" if e.get("tk") else "")
                + f"<span class=muted>{html.escape(meta)}</span><span style='margin-left:auto'>{closebtn}</span></div>"
                f"<div class=jrow><b>Thesis</b>{html.escape(e.get('thesis',''))}</div>"
                + (f"<div class=jrow><b>Expect</b>{html.escape(e.get('expect',''))}</div>" if e.get("expect") else "")
                + (f"<div class=jrow><b>Kills it</b>{html.escape(e.get('kill',''))}</div>" if e.get("kill") else "")
                + outcome + "</div>")
    opens = [e for e in decs if e.get("status") != "closed"]
    closed = [e for e in decs if e.get("status") == "closed"]
    dform = ("<form class=jform onsubmit='return jdAdd(event)'>"
             "<div class=jformrow><input id=jd-tk class=jtk list=tklist placeholder='TKR' maxlength=8 autocapitalize=characters>"
             "<select id=jd-action><option>buy</option><option>add</option><option>trim</option><option>sell</option>"
             "<option>hold</option><option>pass</option></select>"
             "<input id=jd-qty class=jnum placeholder='qty'><input id=jd-price class=jnum placeholder='price'>"
             "<input id=jd-source placeholder='source (digest date / own idea)' style='flex:1'></div>"
             "<textarea id=jd-thesis rows=2 placeholder='Thesis — why this, why now (required)'></textarea>"
             "<textarea id=jd-expect rows=1 placeholder='What I expect, over what horizon'></textarea>"
             "<textarea id=jd-kill rows=1 placeholder='What proves me wrong / kills this'></textarea>"
             "<button class='btn primary' type=submit>Log decision</button></form>")
    dec_w = ("<div class=widget><div class=whead><span class=wtitle>Decision log</span>"
             f"<span class=wcount>{len(opens)} open · {len(closed)} closed</span></div><div class='wbody pad'>"
             "<div id=unj></div>" + dform
             + ("<div class=sec>Open</div>" + "".join(_dcard(e) for e in sorted(opens, key=lambda x: x.get("date",""), reverse=True)) if opens else "")
             + (f"<details class=quietlog style='margin-top:10px'><summary>Closed <span class=wcount>{len(closed)}</span></summary>"
                + "".join(_dcard(e) for e in sorted(closed, key=lambda x: x.get("date",""), reverse=True)) + "</details>" if closed else "")
             + "</div></div>")
    return head + note_w + dec_w

def feed_item(date, badge, desc, url, src="", summary=""):
    s = f"<span class=fsrc>{html.escape(src)}</span>" if src else ""
    sm = f"<div class=fsum>{html.escape(summary)}</div>" if summary else ""
    return (f"<div class=fitem><div class=fhead>"
            f"<span class=fdate>{html.escape(date)}</span>{badge}{s}<span class=fdesc>{html.escape(desc)}</span></div>"
            f"<div class=fbody>{sm}<a class=fopen href='{html.escape(url)}' target=_blank rel=noopener>Open ↗</a></div></div>")

def stat_cell(k, v, vid=""):
    idattr = f" id='{vid}'" if vid else ""
    return f"<div class=stat><div class=stk>{k}</div><div class=stv{idattr}>{v}</div></div>"

def ticker_inner(sym):
    sym = sym.upper()
    pr = profile(sym); cik = cik_of(sym)
    name = html.escape(pr.get("name") or sym)
    meta = " · ".join(html.escape(x) for x in [pr.get("sector"), pr.get("industry"), pr.get("country")] if x)
    watching = sym in watchlist()
    wbtn = (f"<button class='wbtn{' on' if watching else ''}' onclick='toggleWatch(event,\"{sym}\")'>"
            f"{'✓ Watching' if watching else '+ Watchlist'}</button>")
    rbtn = f"<button class=rbtn2 onclick='genResearch(event,\"{sym}\")'>✦ Deep research</button>"
    ranges = "".join(f"<button class='rbtn{' on' if r == '6mo' else ''}' data-range='{r}'>{lbl}</button>"
                     for r, lbl in [("1w", "1W"), ("1mo", "1M"), ("3mo", "3M"), ("6mo", "6M"),
                                    ("ytd", "YTD"), ("1y", "1Y"), ("5y", "5Y")])
    pos = read_positions().get(sym, {})
    sh = pos.get("shares") or 0; cb = pos.get("cost_basis") or 0
    body = [f"<div class=thead><div><h1 class=doctitle>{name} <span class=tksym>{sym}</span></h1>"
            f"<div class='muted tmeta'>{meta}</div></div><div class=theadbtns>{wbtn}{rbtn}</div></div>",
            market_header(sym, spark=False),
            f"<div class=pchart data-ticker='{sym}'><div class=ranges>{ranges}</div>"
            f"<div class=pfsum2 data-pxsum></div><div class=chartbox><canvas></canvas></div></div>"]
    # key stats (filled live from /api/quote)
    stats = [stat_cell("Previous close", "—", "st-prev"), stat_cell("Day range", "—", "st-day"),
             ("<div class='stat wide'><div class=stk>52-week range</div><div class=stv id=st-52>"
              "<span class=rlo>—</span><span class=rbar><span class=rmark></span></span><span class=rhi>—</span></div></div>"),
             stat_cell("Market cap", "—", "st-cap")]
    if sh > 0:
        stats += [stat_cell("Your shares", f"{sh:,.0f}"), stat_cell("Avg cost", f"${cb:,.2f}"),
                  stat_cell("Market value", "—", "st-mv"), stat_cell("Unrealized P&amp;L", "—", "st-pnl")]
    if pos.get("next_catalyst"):
        stats.append(f"<div class='stat wide'><div class=stk>Next catalyst</div><div class=stv>{html.escape(pos['next_catalyst'])}</div></div>")
    body.append(f"<div class=statgrid data-stats='{sym}' data-shares='{sh}' data-cost='{cb}'>{''.join(stats)}</div>")
    body.append(f"<div id=research-status data-ticker='{sym}' class=rstatus style='display:none'></div>")

    cf = company_folder(sym)
    # ---- tab panes ----
    over = []
    folds = []  # heavy detail folded below the card (lenses, flags, business)
    if cf:
        card = render_card(cf)
        if card:
            over.append(card)
        fr = cf / "analysis" / "FINAL-REPORT.md"
        if fr.exists():
            rel = fr.relative_to(ROOT)
            # staleness: report age + what's been filed since
            rdate = dt.date.fromtimestamp(fr.stat().st_mtime)
            since = [f for f in (edgar_filings(cik) if cik else []) if f["date"] > rdate.isoformat()]
            nmat = sum(1 for f in since if f["material"] or f["form"].startswith("10-"))
            age = (dt.date.today() - rdate).days
            if nmat or age > 45:
                scls, stxt = "warn", f"{nmat} material filing{'s' if nmat != 1 else ''} since" if nmat else "aging — consider an update"
            elif since:
                scls, stxt = "neu", f"{len(since)} routine filings since"
            else:
                scls, stxt = "buy", "current"
            ne = next_earnings(sym)
            if sh > 0:
                sched = f"auto-refreshes the morning after earnings{f' · next: {ne}' if ne else ''}"
            else:
                sched = f"manual refresh on thesis triggers{f' · next earnings: {ne}' if ne else ''}"
            over.append(f"<div class=stale><span class='pill {scls}'>{stxt}</span>"
                        f"<span class=muted>Report dated {rdate.isoformat()} · {age}d old · {html.escape(sched)}</span>"
                        f"<button class=wbtn onclick='updResearch(event,\"{sym}\")'>↻ Update research</button></div>")
            over.append(f"<a class='btn primary' data-path='{rel}' href='/view?path={rel}'>Open final report</a>")
        ln = render_lenses(cf)
        if ln:
            folds.append(f"<details class=fold><summary>Investor lenses <span class=hint>(multiple philosophies, one read)</span></summary>{ln}</details>")
        ff = financial_flags(cf)
        if ff:
            folds.append(f"<details class=fold><summary>Financial highlights & flags</summary>{ff}</details>")
    if sh > 0:
        try:
            lv = json.loads((CONF / "triggers.json").read_text()).get("price_levels", {}).get(sym, {})
        except Exception:
            lv = {}
        if lv:
            prows = []
            if lv.get("above"):
                prows.append(f"<div class=planrow><span class=plk>Upper — trim / sell zone</span>"
                             f"<span class=plv>${lv['above']:,.2f}</span><span class='pld' data-lvl='{lv['above']}'>—</span></div>")
            if lv.get("below"):
                prows.append(f"<div class=planrow><span class=plk>Lower — add / re-check zone</span>"
                             f"<span class=plv>${lv['below']:,.2f}</span><span class='pld' data-lvl='{lv['below']}'>—</span></div>")
            note = f"<div class=plnote>{html.escape(lv.get('note',''))}</div>" if lv.get("note") else ""
            oo = [o for o in _json("open_orders.json", []) if o.get("tk") == sym]
            for o in oo:
                prows.append(f"<div class=planrow><span class='plk working'>⏳ Working — {html.escape(o.get('side',''))} "
                             f"{o.get('qty'):,} <span class=hint>(GTC, placed {html.escape(o.get('placed',''))})</span></span>"
                             f"<span class=plv>${o.get('limit'):,.2f}</span><span class='pld' data-lvl='{o.get('limit')}'>—</span></div>")
            if oo:
                note += ("<div class='plnote hint2'>Working orders are as YOU declared them (journal, 8/10) — BROKERA's API "
                         "doesn't expose open orders; fills appear in Purchase history automatically when they execute.</div>")
            over.append("<div class=sec>Trade plan <span class=hint>(pre-set levels — place as GTC limit orders; "
                        "your phone pings when either level is crossed)</span></div>"
                        f"<div class=planwrap>{''.join(prows)}{note}</div>")
        over.append(f"<div class=sec>Purchase history <span class=hint>(tranches · long-term CG clock)</span></div>"
                    f"<div id=lots data-lots='{sym}' data-cost='{cb}'><p class=muted>loading…</p></div>")
    if pr.get("summary"):
        folds.append(f"<details class=fold><summary>Business</summary><p class=summary>{html.escape(pr['summary'][:900])}…</p></details>")
    over += folds
    if not over:
        over.append("<p class=muted>No research on file yet — hit ✦ Deep research to generate the full dossier, or check Filings and News.</p>")

    filings, nfil = [], 0
    if cik:
        fl = edgar_filings(cik)
        nfil = len(fl)
        if fl:
            filings.append("<div class=feed>")
            for f in fl:
                badge = "<span class='badge earn'>Earnings</span>" if f["earnings"] else ("<span class='badge mat'>8-K</span>" if f["material"] else f"<span class=badge>{html.escape(f['form'])}</span>")
                filings.append(feed_item(f["date"], badge, f["desc"] or f["form"], f["url"]))
            filings.append("</div>")
        else:
            filings.append("<p class=muted>No filings returned by EDGAR.</p>")
    else:
        filings.append("<p class=muted>No SEC CIK found — likely a foreign ADR not in the SEC ticker map.</p>")

    news = finnhub_news(sym); newsp, nnews = [], 0
    if news is None:
        newsp.append("<p class=muted>Add a Finnhub key to enable news.</p>")
    elif not news:
        newsp.append("<p class=muted>No recent news (Finnhub coverage is thin for micro-caps — use the Filings tab).</p>")
    else:
        nnews = len(news)
        bydate = {}
        for n in news:
            d = dt.datetime.utcfromtimestamp(n.get("datetime", 0)).strftime("%Y-%m-%d") if n.get("datetime") else "—"
            bydate.setdefault(d, []).append(n)
        newsp.append("<div class=feed>")
        for d in sorted(bydate, reverse=True):
            items = bydate[d]; top = items[0]
            more = f" <span class=fcount>+{len(items)-1} more</span>" if len(items) > 1 else ""
            links = "".join(f"<a class=fnlink href='{html.escape(x.get('url',''))}' target=_blank rel=noopener>"
                            f"<span class=fnsrc>{html.escape(x.get('source',''))}</span>{html.escape(x.get('headline',''))}</a>"
                            for x in items)
            newsp.append(f"<div class=fitem><div class=fhead><span class=fdate>{html.escape(d)}</span>"
                         f"<span class=fdesc>{html.escape(top.get('headline',''))}{more}</span></div>"
                         f"<div class=fbody>{links}</div></div>")
        newsp.append("</div>")

    research = []
    if cf:
        def _doclist(files):
            rows = []
            for f in files:
                rel = f.relative_to(ROOT)
                rows.append(f"<a class=docrow data-path='{rel}' href='/view?path={rel}'>{I_DOC}"
                            f"<span class=dlabel>{html.escape(label(f))}</span><span class=dfile>{html.escape(f.name)}</span></a>")
            return f"<div class=doclist>{''.join(rows)}</div>"
        groups = company_groups(cf)
        for glabel, files in groups.items():
            if glabel == "Reports & analysis":  # the canonical four stay visible
                research.append(f"<div class=sec>{glabel}</div>" + _doclist(files))
            else:  # updates history, deep research, raw financials fold away
                research.append(f"<details class=fold><summary>{glabel} <span class=tcount>{len(files)}</span></summary>"
                                + _doclist(files) + "</details>")
        if sh <= 0:
            research.append(f"<div class=archrow><button class='btn subtle' onclick='archiveResearch(event,\"{sym}\")'>"
                            f"Archive this research</button><span class='muted hint2'>moves the folder to _archive/ — reversible, "
                            f"clears it from the sidebar</span></div>")

    tabs = ["<div class=tabs>",
            "<button class='tab on' data-tab=t-over>Overview</button>",
            f"<button class=tab data-tab=t-fil>Filings{f' <span class=tcount>{nfil}</span>' if nfil else ''}</button>",
            f"<button class=tab data-tab=t-news>News{f' <span class=tcount>{nnews}</span>' if nnews else ''}</button>"]
    if research:
        tabs.append("<button class=tab data-tab=t-res>Research library</button>")
    tabs.append("</div>")
    body += tabs
    body.append(f"<div class='pane on' id=t-over>{''.join(over)}</div>")
    body.append(f"<div class=pane id=t-fil><div class=sec>Recent SEC filings <span class=hint>(8-K = material events / earnings)</span></div>{''.join(filings)}</div>")
    body.append(f"<div class=pane id=t-news>{''.join(newsp)}</div>")
    if research:
        body.append(f"<div class=pane id=t-res>{''.join(research)}</div>")
    return "".join(body)

@app.route("/")
def home():
    return wrap("Personal book", home_inner(), request.args.get("partial"))

@app.route("/company/<name>")
def company(name):
    d = safe(name)
    for pref in ["analysis/FINAL-REPORT.md", "analysis/soft-research-dossier.md"]:
        if (d / pref).exists():
            rel = (d / pref).relative_to(ROOT)
            return wrap(label(d / pref), view_inner(rel), request.args.get("partial"))
    abort(404)

@app.route("/view")
def view():
    return wrap(label(safe(request.args.get("path", ""))), view_inner(request.args.get("path", "")), request.args.get("partial"))

@app.route("/ticker/<sym>")
def ticker(sym):
    return wrap(sym.upper(), ticker_inner(sym), request.args.get("partial"))

@app.route("/recommendation")
def recommendation():
    return wrap("Advice", rec_inner(), request.args.get("partial"))

@app.route("/journal")
def journal():
    return wrap("Journal", journal_inner(), request.args.get("partial"))

_QCACHE = {}
_QMETA = {}   # market cap + 52-week range: slow-moving, so refreshed every meta_s, not every price tick

# .renew on a price-warmer thread: whether THIS fetch may renew cap/52-week (the budget had room
# for it this tick). Unset on a page's request thread.
_WARMING = threading.local()

def _meta_due(t):
    """Past meta_s — plain, not _ttl(): off-market _ttl() would make the post-close pass renew every
    name's range, and the open expire all of them again 17.5 h later."""
    return not (t in _QMETA and time.time() - _QMETA[t][0] < LIVE["meta_s"])

def _quote_meta(t, key):
    """Market cap + 52-week range: two Finnhub calls. The price warmer renews a tracked name's when
    it is due AND the budget has room after every due price (_quotes_affordable); until then the
    old range stands, and a name that has none waits for one. A page's own fetch reuses what is on
    hand, however old, for a tracked name (so a page opened at 9:30 does not triple its quote
    calls), and fetches it for an untracked name that has none or one past meta_s — nothing else
    would ever renew that one."""
    now = time.time()
    renew = getattr(_WARMING, "renew", None)
    if renew is None:
        if t in _QMETA and (not _meta_due(t) or t in _sets()[1]):
            return _QMETA[t][1]
    elif not (renew and _meta_due(t)):
        return _QMETA.get(t, (0.0, {}))[1]
    d = {}
    try:
        _fh_note("meta")
        p2 = requests.get("https://finnhub.io/api/v1/stock/profile2", params={"symbol": t, "token": key}, timeout=10).json()
        if p2.get("marketCapitalization"):
            d["cap"] = p2["marketCapitalization"] * 1e6
    except Exception:
        pass
    try:
        _fh_note("meta")
        m = requests.get("https://finnhub.io/api/v1/stock/metric", params={"symbol": t, "metric": "price", "token": key}, timeout=10).json().get("metric", {})
        d["yhi"], d["ylo"] = m.get("52WeekHigh"), m.get("52WeekLow")
    except Exception:
        pass
    if d:   # a failed lookup is retried on the next price refresh, not remembered for 6h
        _QMETA[t] = (now, d)
    return d
def research_inner():
    """/research — top-level since 2026-09-22 (David: "we don't need all the watchlist/research
    stuff in left hand side, these should be embedded somewhere, maybe a dedicated research tab").
    This seg is the Watchlist: the bench of watched and written-up names we don't own, with add /
    remove, plus What's new and Coming up. Companies and Boards are its sibling segs. The
    #digest/#calendar loaders run on any page — the widgets live here, their JS followed."""
    skel = "<span class=skel style='width:60%'></span>"
    digest_filters = ("<div class=ffilters><button class='fbtn on' data-ff=all>All</button>"
                      "<button class=fbtn data-ff=filing>Filings</button>"
                      "<button class=fbtn data-ff=news>News</button></div>")
    cal_w = ("<div class=widget><div class=whead><span class=wtitle>Coming up "
             "<span class=hint>— earnings & catalysts, held + watched</span></span></div>"
             f"<div id=calendar class=feed><div class=fitem style='padding:14px 18px'>{skel}</div></div></div>")
    dig_w = (f"<div class=widget><div class=whead><span class=wtitle>What's new</span>{digest_filters}</div>"
             f"<div id=digest class=feed><div class=fitem style='padding:14px 18px'>{skel}</div></div></div>")
    add = ("<form class=addbar onsubmit='return doAdd(event)'><input id=addtk placeholder='Add ticker…' "
           "autocomplete=off spellcheck=false aria-label='Add a ticker to the watchlist'>"
           "<button type=submit>Add</button></form>")
    return ("<div class=pagehead><div><h1>Research</h1><p class=muted>Names we watch or have written up "
            "but don't own, how each has traded, and what's new. The watchlist also feeds What's new "
            "and the Brief. What we own is under Holdings.</p></div>"
            f"<div class=headactions>{add}</div></div>"
            "<div class=homegrid><div class=homemain>" + names_w() + dig_w +
            "</div><div class=homerail>" + cal_w + "</div></div>")


def _writeup(cf):
    """The best write-up link for a research folder, or '' (FINAL-REPORT, else the dossier)."""
    if cf:
        for pref in ("analysis/FINAL-REPORT.md", "analysis/soft-research-dossier.md"):
            if (cf / pref).exists():
                rel = html.escape(str((cf / pref).relative_to(ROOT)))
                return f"<a class=doclink data-path='{rel}' href='/view?path={rel}'>Dossier</a>"
    return ""


def names_w():
    """The tracked-names table (David 2026-08-13: "research tab should have the watchlist
    stocks and their performance along with research stocks"). One row per name we watch or
    have researched but do NOT own in any book — owned names live under Holdings. × stops
    watching a name; + Watch adds a researched one to the watchlist feed. Prices and returns
    arrive from the single /api/perf batch."""
    pos = read_positions()
    held = held_all()
    wl = watchlist()
    names = [t for t in tracked_tickers() if t not in held]
    rows = ""
    for tk in names:
        cf = company_folder(tk)
        ov = lenses_overall(cf) if cf else ""
        verdict = f"<span class='pill {sig_cls(ov)}'>{html.escape(ov)}</span>" if ov else "<span class=muted>—</span>"
        # folder names are CamelCase slugs ("NACCOIndustries") — SEC's title reads better
        nm = html.escape(pos.get(tk, {}).get("name") or ticker_names().get(tk)
                         or (cf.name.rsplit("-", 1)[0].replace("_", " ") if cf else ""))
        tag = "<span class=tag>Watching</span>" if tk in wl else "<span class='tag res'>Research</span>"
        # one toggle per row under a "Watch" header: ✓ = on the watchlist feed (tap to stop), + = add.
        # A bare "+" beside "×" buttons in an unlabelled column read as two unrelated actions on a phone.
        act = (f"<button class='wbtn on' title='Stop watching {tk}' aria-label='Watching {tk}: tap to stop' "
               f"aria-pressed=true onclick='rsUnwatch(event,\"{tk}\")'>✓<span class=wl> Watching</span></button>"
               if tk in wl else
               f"<button class=wbtn title='Watch {tk}' aria-label='Watch {tk}' aria-pressed=false "
               f"onclick='rsWatch(event,\"{tk}\")'>+<span class=wl> Watch</span></button>")
        rows += (f"<tr class=nrow data-tk='{tk}'>"
                 f"<td><a class=tklink data-tk='{tk}' href='/ticker/{tk}'>{tk}</a>{tag}"
                 f"<div class=subname>{nm}</div></td>"
                 f"<td class='num p-price c-px'>·</td><td class='num p-d1 c-d1'></td><td class='num p-m1 c-wide'></td>"
                 f"<td class='num p-m6 c-mid'></td><td class='num p-ytd c-wide'></td>"
                 f"<td class=c-mid>{verdict}</td><td class=c-wide>{_writeup(cf) or '<span class=muted>—</span>'}</td>"
                 f"<td class=rmc>{act}</td></tr>")
    # owned names that are still on the watchlist file keep feeding What's new and the Brief;
    # the old sidebar let you drop them, so this line still does
    owned = [t for t in wl if t in held]
    foot = ("<div class=wfoot>Owned and still on the watchlist feed: " + " ".join(
        f"<span class=wchip><a data-tk='{t}' href='/ticker/{t}'>{t}</a><button class=rm title='Stop watching {t}' "
        f"aria-label='Stop watching {t}' onclick='rsUnwatch(event,\"{t}\")'>×</button></span>" for t in owned)
        + "</div>") if owned else ""
    empty = ("" if names else "<div class=pfempty2>Nothing on the bench — add a ticker above, or open one "
             "from a candidate board.</div>")
    return ("<div class=widget><div class=whead><span class=wtitle>Watchlist "
            "<span class=hint>— watched and written up, not owned</span></span>"
            f"<span class=wcount>{len(names)}</span></div><div class=wbody>"
            "<table class=dt id=nametable><thead><tr><th>Name</th><th class='num c-px'>Price</th>"
            "<th class='num c-d1'>Day</th><th class='num c-wide'>1M</th><th class='num c-mid'>6M</th>"
            "<th class='num c-wide'>YTD</th><th class=c-mid>Verdict</th><th class=c-wide>Write-up</th>"
            "<th class=rmc>Watch</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>{empty}{foot}</div></div>")


def research_companies_inner():
    """/research/companies — every research folder (what the sidebar's Research tree was):
    grouped Held / Watching / Researched, the verdict dot, the report / playbook / latest update,
    the live ticker, and archive (never for a name any book owns)."""
    held = held_all(); wl = set(watchlist()); pos = read_positions()
    groups = {"Held": [], "Watching": [], "Researched": []}
    for c in companies():
        tk = ticker_of(c)
        nm = (pos.get(tk, {}).get("name") or ticker_names().get(tk)
              or c.name.rsplit("-", 1)[0].replace("_", " "))
        groups["Held" if tk in held else ("Watching" if tk in wl else "Researched")].append((tk, nm, c))
    out = []
    for g, items in groups.items():
        if not items:
            continue
        out.append(f"<div class=rgrp-h>{g} <span class=hint>{len(items)}</span></div><div class=rco-list>")
        for tk, nm, c in items:
            ov = lenses_overall(c)
            dot = f"<span class='vdot {sig_cls(ov)}'></span>" if ov else "<span class=vdot></span>"
            verdict = html.escape(ov) if ov else "No verdict yet"
            arch = ("" if tk in held else
                    f"<button class='rm rco-x' title='Archive this research' aria-label='Archive the {tk} research' "
                    f"onclick='archiveResearch(event,\"{tk}\")'>×</button>")
            picks = [(c / "analysis" / "FINAL-REPORT.md", "Report"),
                     (c / "analysis" / "trade-playbook.md", "Playbook")]
            ud = c / "analysis" / "updates"
            ups = sorted(ud.glob("update-*.md"), reverse=True) if ud.exists() else []
            if ups:
                picks.append((ups[0], f"Update · {ups[0].stem.replace('update-', '')}"))
            links = "".join(
                f"<a class=rlink data-path='{html.escape(str(f.relative_to(ROOT)))}' "
                f"href='/view?path={html.escape(str(f.relative_to(ROOT)))}'>{html.escape(lab)}</a>"
                for f, lab in picks if f.exists())
            links += f"<a class=rlink data-tk='{tk}' href='/ticker/{tk}'>Live ticker</a>"
            out.append(f"<div class=rco data-co='{tk}'><div class=rco-top>{dot}"
                       f"<a class=rco-nm data-tk='{tk}' href='/ticker/{tk}'>{html.escape(nm)}</a>"
                       f"<span class=rco-tk>{tk}</span>{arch}</div>"
                       f"<div class=rco-v>{verdict}</div><div class=rco-links>{links}</div></div>")
        out.append("</div>")
    if not out:
        out.append("<div class=pfempty2>No dossiers yet — open a ticker and run ✦ Research.</div>")
    return ("<div class=pagehead><div><h1>Companies</h1><p class=muted>Every company with a research "
            "folder. The dot is the verdict: green buy, amber buy-below or caution, red pass. "
            "× archives a folder (it moves to _archive/, reversible).</p></div></div>" + "".join(out))


def research_boards_inner():
    """/research/boards — the candidate boards (the weekly sourcing scan, newest first), a new
    scan on demand, and the system docs folded underneath."""
    rows = []
    for b in boards():
        rel = html.escape(str(b.relative_to(ROOT)))
        rows.append(f"<a class=docrow data-path='{rel}' href='/view?path={rel}'>{I_DOC}"
                    f"<span class=dlabel>Board · {html.escape(b.stem.replace('board_', ''))}</span></a>")
    lst = (f"<div class=doclist>{''.join(rows)}</div>" if rows else
           "<div class=pfempty2>No boards yet — New board scan runs one now.</div>")
    docs = "".join(
        f"<a class=docrow data-path='{html.escape(str(f.relative_to(ROOT)))}' "
        f"href='/view?path={html.escape(str(f.relative_to(ROOT)))}'>{I_DOC}"
        f"<span class=dlabel>{html.escape(label(f))}</span></a>" for f in sysdocs())
    sysw = (f"<details class=fold><summary>System docs <span class=hint>({len(sysdocs())}) — how the "
            f"desk works</span></summary><div class=doclist>{docs}</div></details>") if docs else ""
    return ("<div class=pagehead><div><h1>Candidate boards</h1><p class=muted>The weekly list of names "
            "being sold for reasons other than value (Mondays, 6:45am ET). Open one to read it; "
            "+ watch on a row adds the name to the watchlist.</p></div>"
            f"<div class=headactions><button class=btn onclick='boardRefresh()'>{I_REFRESH}"
            "<span>New board scan</span></button></div></div>" + lst + sysw)


@app.route("/research")
def research_home():
    return wrap("Research", research_inner(), request.args.get("partial"))


@app.route("/research/companies")
def research_companies():
    return wrap("Companies", research_companies_inner(), request.args.get("partial"))


@app.route("/research/boards")
def research_boards():
    return wrap("Candidate boards", research_boards_inner(), request.args.get("partial"))


@app.route("/research/<sym>")
def research_ticker(sym):
    """/research/<TK> was linked (the industries Companies table) but never existed: send it to
    the ticker page, keeping ?partial so an SPA fetch still gets a partial."""
    from flask import redirect
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,12}", sym):
        abort(404)
    qs = request.query_string.decode()
    return redirect(f"/ticker/{sym.upper()}" + (f"?{qs}" if qs else ""))


@app.route("/api/quotes")
def quotes():
    """Many tickers, one request. The page used to fire one /api/quote per holding and
    per sidebar row, so a phone on the tailnet paid a full round trip for each — and the
    Account widget can't compute until the LAST of them lands. Same cache underneath."""
    tks = [t.strip().upper() for t in request.args.get("tickers", "").split(",") if t.strip()][:40]
    # fan out: serially this was ~1s per cold ticker, so fourteen watchlist names cost
    # 15s and the phone re-requested it three times while waiting (server log 2026-09-03)
    if len(tks) > 1:
        with ThreadPoolExecutor(min(16, len(tks))) as ex:   # Finnhub allows a 30/s burst
            return jsonify(dict(zip(tks, ex.map(_quote, tks))))
    return jsonify({t: _quote(t) for t in tks})

@app.route("/api/quote")
def quote():
    return jsonify(_quote(request.args.get("ticker", "").upper()))

_QBG = set()          # names with a background refresh in flight
_QBG_LOCK = threading.Lock()

_QRETRY = {}          # name -> epoch before which a quote Finnhub refused is not asked for again

def _quote_window(t, now=None):
    """(fresh_s, oldest_s) for a price asked for now, by the live-data policy (LIVE). Younger than
    fresh_s it is served as is; up to oldest_s it is served at once while one background fetch
    refreshes it (stale-while-revalidate: on the slow link a Finnhub call takes 0.5-3s); older, the
    caller waits for a fresh one. oldest_s None = any age, because the market is closed.
      market hours  owned: fresh a tick past owned_every() (the warmer re-quotes it on that beat),
                    never older than owned_max_s. Anything else: fresh until the warmer's turn
                    (other_s − other_lead_s, plus a tick), never older than other_s.
      off-market    fresh iff fetched since the settle point (the post-close pass). An older price
                    is still served at once, and fetched once behind it — never again till the open."""
    m = market(now)
    if m["open"]:
        if t in _sets()[0]:
            every = owned_every()
            return every + LIVE["tick_s"], max(LIVE["owned_max_s"], 2 * every)
        return LIVE["other_s"] - LIVE["other_lead_s"] + LIVE["tick_s"], LIVE["other_s"]
    if not m["settled"]:
        return LIVE["other_s"], None
    return m["now"] - m["settled"], None

def _quote(t):
    """A price by the live-data policy (_quote_window): the page waits only for a name never
    fetched, or one past its oldest_s in market hours. A name Finnhub refused a moment ago keeps
    its last price until its retry falls due — except an owned name in market hours, which is
    never served past oldest_s for it: _quote_fetch prices that one from Yahoo meanwhile."""
    c = _QCACHE.get(t)
    fresh, oldest = _quote_window(t)
    if c:
        age = time.time() - c[0]
        if age < fresh:
            return c[1]
        if c[1].get("price"):
            waiting = time.time() < _QRETRY.get(t, 0)
            if oldest is None or age < oldest:
                if not waiting:
                    _quote_bg(t)
                return c[1]
            if waiting and t not in _sets()[0]:
                return c[1]
    with _cache_lock(f"q:{t}"):   # single-flight: the warmer and a page open share one fetch
        c = _QCACHE.get(t)
        if c and time.time() - c[0] < fresh:
            return c[1]
        return _quote_fetch(t)

def _quote_bg(t):
    with _QBG_LOCK:
        if t in _QBG:
            return
        _QBG.add(t)

    def run():
        try:
            with _cache_lock(f"q:{t}"):
                c = _QCACHE.get(t)
                if not (c and time.time() - c[0] < _quote_window(t)[0]):
                    _quote_fetch(t)
        except Exception:
            pass
        finally:
            with _QBG_LOCK:
                _QBG.discard(t)
    threading.Thread(target=run, daemon=True).start()

_QFAILS = {}          # name -> failed quote fetches in a row (the backoff's exponent)

def _quote_backoff(t, now):
    """A quote Finnhub refused, or that neither source priced: not asked for again before retry_s,
    doubling, retry_tries times in a row — then, off-market, not before the next open (the close
    is as good as tonight gets), and in market hours at the longest of those waits. An owned name
    in market hours always waits just retry_s: Finnhub's window is a minute, and it is our money."""
    n = _QFAILS.get(t, 0) + 1
    _QFAILS[t] = n
    m = market()
    if m["open"] and t in _sets()[0]:
        _QRETRY[t] = now + LIVE["retry_s"]
    elif n > LIVE["retry_tries"] and not m["open"]:
        _QRETRY[t] = m["next"]
    else:
        _QRETRY[t] = now + LIVE["retry_s"] * 2 ** (min(n, LIVE["retry_tries"]) - 1)

def _quote_fetch(t):
    """Finnhub first (cap/52-week from _quote_meta); Yahoo for a symbol Finnhub does not price. A
    refusal (rate limit, blip) keeps the last good price, at its own age, and backs the name off
    (_quote_backoff) rather than stall on Yahoo, which throttles bursts hard — except an owned name
    in market hours whose price is past its oldest_s, which Yahoo prices rather than let it get
    older still (Finnhub is not asked again inside its backoff). A fetch that finds no price
    anywhere never overwrites one we had."""
    now = time.time()
    old = _QCACHE.get(t)
    had = bool(old and old[1].get("price"))
    key = load_key("finnhub"); d = {}
    waiting = now < _QRETRY.get(t, 0)       # inside a refusal's backoff: Finnhub is not asked
    refused = waiting
    if key and not waiting:
        try:
            _fh_note("quote", t)
            r = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": t, "token": key}, timeout=10)
            q = r.json()
            if q.get("c"):
                d = {"price": q.get("c"), "prev": q.get("pc"), "dhi": q.get("h"), "dlo": q.get("l"), "at": round(now)}
                d.update(_quote_meta(t, key))   # one price call per refresh, not three
            else:   # a 200 with c=0 is a symbol Finnhub does not price: that one goes to Yahoo
                refused = r.status_code != 200 or "error" in q
        except Exception:
            d = {}; refused = True
    if refused and had:
        oldest = _quote_window(t)[1]
        overdue = oldest is not None and t in _sets()[0] and now - old[0] >= oldest
        if not overdue:
            if not waiting:
                _quote_backoff(t, now)
            return old[1]
    if not d.get("price"):
        _up("yahoo:quote")
        try:
            import yfinance as yf
            fi = yf.Ticker(t).fast_info
            d = {"price": getattr(fi, "last_price", None), "prev": getattr(fi, "previous_close", None),
                 "cap": getattr(fi, "market_cap", None), "yhi": getattr(fi, "year_high", None), "ylo": getattr(fi, "year_low", None),
                 "at": round(now)}
        except Exception:
            d = {}
    if d.get("price") and not refused:
        _QRETRY.pop(t, None); _QFAILS.pop(t, None)
    elif not waiting:
        _quote_backoff(t, now)
    if not d.get("price") and had:
        return old[1]           # neither source priced it: the last good price stands, at its own age
    _QCACHE[t] = (now, d)   # a name nothing has ever priced is remembered too, so no page re-asks
    return d

@app.route("/api/spark")
def spark():
    t = request.args.get("ticker", "").upper()
    def fetch():
        _up("yahoo:spark")
        try:
            import yfinance as yf
            h = yf.Ticker(t).history(period="3mo")["Close"].dropna().tolist()
            return [round(x, 4) for x in h[-60:]]
        except Exception:
            return _Failed()
    return jsonify(cached(f"spark:{t}", _ttl(LIVE["chart_s"]), fetch))

@app.route("/api/watchlist", methods=["GET", "POST"])
def wl_api():
    # GET only lists; add/remove are POST (same-origin via _net_guard) — the watchlist is the
    # BROKERA feed universe, and one <img src> could edit it on a GET (PLUMB-8, 2026-09-22)
    action = tk = ""
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        action = b.get("action") or request.args.get("action", "")
        tk = str(b.get("ticker") or request.args.get("ticker", "")).upper().strip()
    f = CONF / "watchlist.txt"
    if action == "add" and tk and tk not in watchlist():
        txt = f.read_text() if f.exists() else ""
        if txt and not txt.endswith("\n"):
            txt += "\n"
        f.write_text(txt + tk + "\n")
    elif action == "remove" and tk:
        lines = [l for l in (f.read_text().splitlines() if f.exists() else []) if l.strip().upper() != tk]
        f.write_text("\n".join(lines) + ("\n" if lines else ""))
    return jsonify({"ok": True, "watching": tk in watchlist(), "watchlist": watchlist()})

def _st():
    a, b = load_key("aggregatora_client_id"), load_key("aggregatora_consumer_key")
    if not (a and b):
        return None
    from aggregatora_client import AggregatorA
    return AggregatorA(client_id=a, consumer_key=b)

def _st_user(st):
    uf = CONF / "aggregatora_user.json"
    if uf.exists():
        return json.loads(uf.read_text())
    users = st.authentication.list_snap_trade_users().body  # personal key: pre-provisioned
    if not users:
        return None
    email = users[0]
    rs = st.authentication.reset_snap_trade_user_secret(body={"userId": email})
    u = {"userId": email, "userSecret": rs.body.get("userSecret")}
    uf.write_text(json.dumps(u))
    return u

def _pos_kind(p):
    """AggregatorA instrument type code for a position ('cs', 'et', 'oef', 'bnd', …)."""
    sy = (p.get("symbol") or {}).get("symbol") or {}
    ty = (sy.get("type") or {}) if isinstance(sy, dict) else {}
    return (ty.get("code") or "").lower() if isinstance(ty, dict) else ""

def _is_cash_equiv(p):
    """Stable-NAV money-market funds. AggregatorA's own `cash_equivalent` flag is False
    for them (checked live on VHPXX, Aug 13 2026), and they carry an equity type code
    ('oef'), so the account widget booked the whole Treasury MMF holding as *stocks*. The
    defining property is the one we can test: an open-ended fund whose NAV is pinned
    at $1.00. Trade-feed side does the same test in _cash_equiv()."""
    if _pos_kind(p) not in ("oef", "mmf", "mmkt"):
        return False
    price, avg = float(p.get("price") or 0), float(p.get("average_purchase_price") or 0)
    return round(price, 2) == 1.00 and (not avg or round(avg, 2) == 1.00)

def _bnd_scale(p):
    """Bonds/notes quote as a percent of par, but AggregatorA's *unit* convention for the
    same position has changed under us: Aug 11 2026 it reported units = face dollars
    (100,000 @ 104.15 -> units*price overstated 100x), Aug 13 it reported units =
    $100-par lots (1,000 @ 104.22, where units*price is already exact). Don't guess —
    open_pnl is the broker's own P&L on the position, so pick the scale that
    reproduces it. Falls back to percent-of-par when the broker gives us no P&L."""
    units, price = float(p.get("units") or 0), float(p.get("price") or 0)
    avg, opl = float(p.get("average_purchase_price") or 0), p.get("open_pnl")
    if opl is not None and avg and units and price:
        raw = (price - avg) * units
        if abs(raw - float(opl)) <= abs(raw / 100.0 - float(opl)):
            return 1.0
    return 0.01

def _pos_value(p):
    """Market value of a position, honouring the instrument's price convention."""
    units, price = float(p.get("units") or 0), float(p.get("price") or 0)
    return units * price * (_bnd_scale(p) if _pos_kind(p) == "bnd" else 1.0)

_LAST_SYNC = [0.0]

def _sync_accounts(st, qp, accts, bdir, watch):
    """Pull one BOOK's accounts into <bdir>/account.json + positions.json. Split out of
    st_sync 2026-09-11 when the DIP Venture account joined the same AggregatorA user:
    accounts are routed by _engine/books.py, never summed blindly. `watch` — only the
    BROKERA book feeds names into watchlist.txt (David's universe); other books keep their
    holdings to their own page."""
    def _jread(name, default):
        try:
            return json.loads((bdir / name).read_text())
        except Exception:
            return default
    holdings = {}; total_value = cash = stock_val = note_live = mmf_held = 0.0; note_mark = None
    mmf_names = []
    for acc in accts:
        aid = acc.get("id")
        total_value += float(((acc.get("balance") or {}).get("total") or {}).get("amount") or 0)
        try:
            for b in st.account_information.get_user_account_balance(query_params=qp, path_params={"accountId": aid}).body:
                cash += float(b.get("cash") or 0)
        except Exception:
            pass
        poss = st.account_information.get_user_account_positions(query_params=qp, path_params={"accountId": aid}).body
        for p in poss:
            s = p.get("symbol") or {}; sy = s.get("symbol") or {}
            tk = (sy.get("symbol") if isinstance(sy, dict) else sy)
            if _pos_kind(p) == "bnd":  # structured note: own Account row, never a quotable holding
                note_live += _pos_value(p); note_mark = float(p.get("price") or 0) or note_mark
                continue
            if _is_cash_equiv(p):  # money-market fund: cash, not equity — Account row, not a holding
                mmf_held += _pos_value(p)
                if tk:
                    mmf_names.append(tk.upper())
                continue
            stock_val += _pos_value(p)
            if not tk:
                continue
            tk = tk.upper()
            desc = (sy.get("description") if isinstance(sy, dict) else "") or ""
            h = holdings.setdefault(tk, {"shares": 0.0, "cost_basis": 0.0, "name": desc})
            h["shares"] += float(p.get("units") or 0)
            h["cost_basis"] = float(p.get("average_purchase_price") or 0)
    acct = _jread("account.json", {})
    # the note is worth whatever the broker marks it at when itemized; else fall back to the manual mark
    note_v = round(note_live, 2) if note_live else acct.get("structured_note", 0)
    # money market = the itemized stable-NAV funds PLUS whatever the broker total holds
    # that no position explains (an un-itemized sweep). Keeping them separate matters:
    # the itemized part is a fact, the residual is a plug, and the widget says which.
    resid = round(total_value - cash - stock_val - note_v - mmf_held, 2)
    # A big negative residual means our valuation disagrees with the broker — keep the
    # last good number rather than poison the value chart.
    warn = ""
    tol = max(100.0, 0.01 * total_value)
    if resid < -tol:
        warn = f"positions exceed broker total by ${-resid:,.0f} — money market left at last good value"
        mmf = acct.get("money_market", 0); resid = 0.0
    else:
        if resid < 0:
            resid = 0.0  # rounding / stale-mark noise
        mmf = round(mmf_held + resid, 2)
    acct["mmf_holdings"] = sorted(set(mmf_names))
    acct["mmf_residual"] = resid
    from zoneinfo import ZoneInfo
    now_et = dt.datetime.now(ZoneInfo("America/New_York"))
    acct.update({"total_value": round(total_value, 2), "cash": round(cash, 2), "money_market": mmf,
                 "currency": "USD", "as_of": now_et.strftime("%b %-d, %-I:%M %p ET"),
                 "as_of_epoch": round(time.time()),
                 "accounts": [{"id": a.get("id"), "number": str(a.get("number") or "")[-4:], "name": a.get("name")} for a in accts]})
    if note_live:
        acct["structured_note"] = note_v
        base = re.split(r"\s*·\s*marked\b", acct.get("structured_note_label", ""))[0]
        acct["structured_note_label"] = (base or "Structured note") + (
            f" · marked {note_mark:.2f} live {now_et.strftime('%b %-d')}" if note_mark else "")
    acct.setdefault("mmf_yield", 0.043)
    (bdir / "account.json").write_text(json.dumps(acct, indent=2))
    cur = _jread("positions.json", {})
    for tk in acct["mmf_holdings"]:
        cur.pop(tk, None)  # a money-market fund booked as a holding by an older sync
    for tk, h in holdings.items():
        m = cur.get(tk, {})
        m.update({"shares": round(h["shares"], 4), "cost_basis": round(h["cost_basis"], 4), "status": "Held"})
        m.pop("sold", None)
        if not m.get("name"):
            m["name"] = h["name"]
        cur[tk] = m
    # a Held name the broker no longer reports was sold out (brokera-5, 2026-09-22): zero it and
    # mark it Sold, keeping the row's thesis/catalyst notes. An EMPTY reply is never read as
    # a full liquidation — one bad AggregatorA answer must not wipe the book.
    if holdings:
        for tk, m in cur.items():
            if tk not in holdings and m.get("status") == "Held":
                m.update({"shares": 0, "status": "Sold", "sold": dt.date.today().isoformat()})
    (bdir / "positions.json").write_text(json.dumps(cur, indent=2))
    if watch:
        wl = set(watchlist())
        if set(acct["mmf_holdings"]) & wl:
            wf = CONF / "watchlist.txt"
            wf.write_text("".join(l for l in wf.read_text().splitlines(keepends=True)
                                  if l.strip().upper() not in acct["mmf_holdings"]))
            wl -= set(acct["mmf_holdings"])
        new = [t for t in holdings if t not in wl]
        if new:
            wf = CONF / "watchlist.txt"; txt = wf.read_text() if wf.exists() else ""
            if txt and not txt.endswith("\n"):
                txt += "\n"
            wf.write_text(txt + "\n".join(new) + "\n")
    return {"synced": sorted(holdings.keys()), "warn": warn}

@app.route("/api/aggregatora/sync", methods=["POST"])
def st_sync():
    st = _st()
    if not st:
        return jsonify({"msg": "Add AggregatorA keys to keys.json first."})
    # cooldown: auto-sync on page open shouldn't hammer AggregatorA; manual button passes force=1
    if not request.args.get("force") and time.time() - _LAST_SYNC[0] < 300:
        return jsonify({"ok": True, "skipped": True, "msg": "Synced moments ago."})
    # off-market (LIVE): the automatic sync runs only when the snapshot predates the last close —
    # nothing at the broker moves overnight. The page's autoSync() asks the same; this holds it for
    # a tab running yesterday's script. The Sync button (force=1) always goes.
    if not request.args.get("force"):
        m = market()
        if not m["open"] and float(_json("account.json", {}).get("as_of_epoch") or 0) >= m["close"]:
            return jsonify({"ok": True, "skipped": True, "msg": "Market closed — synced since the close."})
    _LAST_SYNC[0] = time.time()
    try:
        u = _st_user(st)
        if not u:
            return jsonify({"msg": "No AggregatorA user provisioned — connect a brokerage in AggregatorA first."})
        qp = {"userId": u["userId"], "userSecret": u["userSecret"]}
        accts = st.account_information.list_user_accounts(query_params=qp).body
        # 2026-09-11: route every account to its BOOK (config/books.json) — the DIP Venture
        # account lives under the same AggregatorA user as the BROKERA book and must never be
        # summed into it. An unclaimed account is reported, not guessed at (one exception:
        # a pending `books.py connect <book>` claims the single new account it produced).
        sys.path.insert(0, str(Path(__file__).resolve().parents[1])); import books as _books
        bk = _books.load()
        by_book, unassigned = _books.route(accts, bk)
        warns = []
        if unassigned:
            acc, slug = _books.settle_pending(unassigned, bk)
            if acc:
                bk = _books.load(); by_book, unassigned = _books.route(accts, bk)
                warns.append(f"account …{str(acc.get('number') or '')[-4:]} routed to {bk[slug]['label']}")
        synced = {}
        for slug, group in by_book.items():
            if not group:
                continue
            r = _sync_accounts(st, qp, group, _books.book_dir(slug, bk), watch=(slug == "brokera"))
            synced[slug] = r["synced"]
            if r["warn"]:
                warns.append(f"{bk[slug]['label']}: {r['warn']}")
        una = [{"id": a.get("id"), "number": str(a.get("number") or "")[-4:], "name": a.get("name"),
                "total": ((a.get("balance") or {}).get("total") or {}).get("amount")} for a in unassigned]
        if una:
            warns.append("unassigned account(s) " + ", ".join("…" + a["number"] for a in una)
                         + " — python3 _engine/books.py assign <id> <book>")
        warn = "; ".join(warns)
        return jsonify({"ok": True, "synced": synced.get("brokera", []), "books": synced, "unassigned": una,
                        **({"warn": warn} if warn else {})})
    except Exception as e:
        return jsonify({"msg": "Sync error: " + str(getattr(e, "body", e))[:160]})

def _digest_items():
    """Filings + news across the watchlist. One ticker per worker: serially this was
    EDGAR + Finnhub for each of fourteen names, 42s cold (measured 2026-09-03). A source that
    failed for any name marks the digest failed too, so it is rebuilt once that name is retried."""
    failed = []
    def one(tk):
        items = []
        cik = cik_of(tk)
        if cik:
            fl = edgar_filings(cik, 8)
            if isinstance(fl, _Failed):
                failed.append(tk)
            for f in fl:
                items.append({"tk": tk, "date": f["date"], "kind": "filing", "label": f["form"],
                              "earn": f["earnings"], "mat": f["material"], "desc": f["desc"] or f["form"], "url": f["url"]})
        nw = finnhub_news(tk)
        if isinstance(nw, _Failed):
            failed.append(tk)
        if nw:
            for n in nw[:4]:
                d = dt.datetime.fromtimestamp(n.get("datetime", 0), dt.timezone.utc).strftime("%Y-%m-%d") if n.get("datetime") else ""
                items.append({"tk": tk, "date": d, "kind": "news", "label": n.get("source", ""),
                              "earn": False, "mat": False, "desc": n.get("headline", ""), "url": n.get("url", ""),
                              "summary": n.get("summary", "")})
        return items
    wl = watchlist()
    ticker_map()   # populate once before the fan-out
    items = []
    if wl:
        with ThreadPoolExecutor(min(6, len(wl))) as ex:
            for chunk in ex.map(one, wl):
                items.extend(chunk)
    items.sort(key=lambda x: x["date"], reverse=True)
    return _Failed(items[:22]) if failed else items[:22]

@app.route("/api/digest")
def digest():
    # stale=True: an expired digest is shown at once and refreshed behind it
    return jsonify(cached("digest", _ttl(LIVE["news_s"]), _digest_items, stale=True))

def tracked_tickers():
    """Every name the operation follows: owned in any book (the BrokerB agent's positions
    too, so the sidebar's Holdings quotes are pre-warmed), watched, or with a research folder."""
    return sorted(held_all() | set(watchlist()) | {ticker_of(c) for c in companies()})

@app.route("/api/sidebar")
def api_sidebar():
    """The sidebar rendered on its own. It lives OUTSIDE #main, and nav() only ever
    replaces #main — so before this route the tree was built once per full page load
    and never again (David 2026-08-14: "i have to refresh for it to show up in side
    bar... may indicate a deeper architectural issue"). It did: every mutation needed
    its own bespoke DOM surgery, remove/archive had one, add never did, and anything
    nobody hand-wrote surgery for (a finished dossier, a new board, a holding that
    appeared on sync) just went quietly stale. One refresh path replaces all of it."""
    return sidebar()

@app.route("/api/live")
def api_live():
    """The live-data policy as this process sees it now (read-only): the market state and cadences
    the page runs on (MKT), the LIVE table, the Finnhub arithmetic for the current owned + tracked
    set, and every upstream call made since start, by kind."""
    owned, tracked = _sets()
    n_o, n_x = len(owned), len(tracked - owned)
    return jsonify({**market_client(), "live": LIVE, "owned": sorted(owned), "tracked": len(tracked),
                    "finnhub": {"last_min": fh_last_minute(), "owned_last_min": fh_last_minute(owned=True),
                                "owned_beat": owned_beat(), "room": fh_room(), "cap": LIVE["finnhub_cap"],
                                "budget": LIVE["finnhub_budget"],
                                "steady_per_min": round(finnhub_per_min(owned_every(n_o, n_x), n_o, n_x), 1)},
                    "upstream": dict(sorted(_UPSTREAM.items()))})

def _perf_data():
    """Batch performance for every tracked name. One yfinance download covers the whole
    list (~0.3s for ten) — the per-row /api/quote fan-out this replaces was one request
    per ticker and carried no history at all."""
    tks = tracked_tickers()
    if not tks:
        return {}
    _up("yahoo:perf")
    prev = ((_CACHE.get("perf") or (0, None))[1]) or {}
    try:
        import yfinance as yf
        df = yf.download(tks, period="1y", interval="1d", progress=False,
                         auto_adjust=True, threads=True)["Close"]
    except Exception:
        return _FailedDict(prev)
    today = dt.date.today(); out = {}
    for tk in tks:
        try:
            s = (df[tk] if getattr(df, "ndim", 1) > 1 else df).dropna()
        except Exception:
            continue
        if len(s) < 2:
            continue
        last = float(s.iloc[-1])
        def since(cut):  # last close on or before `cut`
            sub = s[s.index.date <= cut]
            return float(sub.iloc[-1]) if len(sub) else None
        def pct(base):
            return round((last / base - 1) * 100, 2) if base else None
        jan = s[s.index.year < today.year]
        out[tk] = {"price": round(last, 2),
                   "d1": pct(float(s.iloc[-2])),
                   "m1": pct(since(today - dt.timedelta(30))),
                   "m6": pct(since(today - dt.timedelta(182))),
                   "ytd": pct(float(jan.iloc[-1])) if len(jan) else None,
                   "y1": pct(float(s.iloc[0])),
                   "lo": round(float(s.min()), 2), "hi": round(float(s.max()), 2)}
    return _perf_merge(tks, out, prev)

def _perf_merge(tks, out, prev):
    """On the slow link Yahoo answers part of a 30-name download with "possibly delisted". A name
    it dropped keeps its last row, and the gap marks the table failed so cached() retries it (a
    name Yahoo has never priced is not a gap, or one bad ticker would retry forever)."""
    missed = [tk for tk in tks if tk not in out and (tk in prev or not prev)]
    if missed:
        return _FailedDict({**{t: prev[t] for t in missed if t in prev}, **out})
    return out

@app.route("/api/perf")
def api_perf():
    # stale=True: a 1y returns table is minutes-old by nature, and on the slow link a cold
    # rebuild is ~14s of yfinance; the pane warmer keeps it within other_s in market hours
    return jsonify(cached("perf", _ttl(LIVE["other_s"]), _perf_data, stale=True))

def _hist_data(t, rng):
    def fetch():
        _up("yahoo:history")
        try:
            import yfinance as yf
            # daily all the way out to 5y (~1200 points, 0.2s) — the client slices one
            # series for every range button, so weekly bars would break 1W and YTD
            cl = yf.Ticker(t).history(period=rng, interval="1d")["Close"].dropna()
            return {"dates": [d.strftime("%Y-%m-%d") for d in cl.index], "closes": [round(float(x), 2) for x in cl.tolist()]}
        except Exception:
            return _FailedDict(dates=[], closes=[])
    return cached(f"hist:{t}:{rng}", _ttl(LIVE["chart_s"]), fetch)

@app.route("/api/history")
def api_history():
    return jsonify(_hist_data(request.args.get("ticker", "").upper(), request.args.get("range", "6mo")))

def _st_activities(st):
    """The account's activity list, fetched once and shared by /api/transactions and
    /api/lots — each used to make its own two AggregatorA round trips (3–6s) for the
    same rows."""
    def fetch():
        _up("aggregatora:activities")
        try:
            u = _st_user(st); qp = {"userId": u["userId"], "userSecret": u["userSecret"]}
            aid = _book_aid(st)
            act = st.account_information.get_account_activities(query_params=qp, path_params={"accountId": aid}).body
            return act if isinstance(act, list) else act.get("data", [])
        except Exception:
            # marked, not raised out of cached(): the key then gets the bounded retry (1, 2, 4 min,
            # then its TTL) — a raise stored nothing, so the pane warmer re-asked AggregatorA every
            # 30 s, overnight too, while a broken connection stayed broken
            return _Failed()
    v = cached(f"st_activities:{_book()}", _ttl(LIVE["activity_s"]), fetch)
    if isinstance(v, _Failed):
        # the callers (transactions, lots, lifetime P&L, the portfolio chart) treat a raise as their
        # own failure; an empty list would read as "no activity" — lifetime net deposits of zero
        raise RuntimeError("AggregatorA activities unavailable (retried on the _Failed schedule)")
    return v

@app.route("/api/transactions")
def api_tx():
    st = _st()
    if not st:
        return jsonify([])
    def fetch():
        try:
            rows = _st_activities(st)
            out = []
            names = ticker_names()
            for a in rows:
                sym = a.get("symbol") or {}
                s = sym.get("symbol") if isinstance(sym, dict) else sym
                sdesc = (sym.get("description") if isinstance(sym, dict) else "") or ""
                tk = (s or "").upper()
                out.append({"date": str(a.get("trade_date") or a.get("settlement_date") or "")[:10],
                            "type": a.get("type"), "symbol": tk, "listed": tk in names,
                            "name": names.get(tk) or sdesc or (a.get("description") or "")[:60],
                            "desc": (a.get("description") or "")[:90],
                            "units": a.get("units"), "price": a.get("price"), "amount": a.get("amount")})
            out.sort(key=lambda x: x["date"], reverse=True)
            return out[:60]
        except Exception:
            return _Failed()
    return jsonify(cached(f"tx:{_book()}", _ttl(LIVE["activity_s"]), fetch))

def _pf_flows(rows):
    """External cash flows by date, from account activities: deposits/withdrawals,
    external wires BROKERA books as INCOME on the sweep CUSIP (e.g. returned funds),
    and in-kind security transfers (OTHER with units; amount = value on transfer date)."""
    flows = {}
    for a in rows:
        ty = (a.get("type") or "").upper()
        d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
        sym = a.get("symbol") or {}
        s = (sym.get("symbol") if isinstance(sym, dict) else sym) or ""
        amt = 0.0
        if ty in ("CONTRIBUTION", "WITHDRAWAL") or (ty == "INCOME" and s == "0USDPRAA7"):
            amt = float(a.get("amount") or 0)
        elif ty == "OTHER" and float(a.get("units") or 0):
            amt = float(a.get("amount") or 0) or float(a.get("units") or 0) * float(a.get("price") or 0)
        if amt and d:
            flows[d] = flows.get(d, 0.0) + amt
    return flows

def _cash_equiv(rows):
    """Money-market funds — tickers whose every trade prints at $1.00 NAV. They are
    cash-equivalents, not equities: quote feeds have no usable history for them, so the
    balance comes from the trade feed. Returns (tickers, {date: running balance})."""
    prices = {}
    for a in rows:
        if (a.get("type") or "").upper() not in ("BUY", "SELL"):
            continue
        sym = a.get("symbol") or {}
        tk = ((sym.get("symbol") if isinstance(sym, dict) else sym) or "").upper()
        p = float(a.get("price") or 0)
        if tk and p:
            prices.setdefault(tk, set()).add(round(p, 4))
    tks = {t for t, ps in prices.items() if ps == {1.0}}
    seen = set(); evs = []
    for a in rows:
        ty = (a.get("type") or "").upper()
        if ty not in ("BUY", "SELL"):
            continue
        sym = a.get("symbol") or {}
        tk = ((sym.get("symbol") if isinstance(sym, dict) else sym) or "").upper()
        d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
        units = float(a.get("units") or 0)
        k = (d, ty, tk, round(units, 4), round(float(a.get("amount") or 0), 2))
        if tk not in tks or not d or k in seen:
            continue
        seen.add(k); evs.append((d, units))
    bal = {}; run = 0.0
    for d, un in sorted(evs):
        run += un; bal[d] = round(run, 2)
    return tks, bal

def _cash_ledger(rows):
    """Running sweep-cash balance from the activity feed: contributions and sales in,
    purchases and withdrawals out, dividends and interest credited. Without this the
    chart shows a phantom dip between selling one holding and settling the next buy."""
    seen = set(); evs = []
    for a in rows:
        ty = (a.get("type") or "").upper()
        if ty not in ("BUY", "SELL", "CONTRIBUTION", "WITHDRAWAL", "DIVIDEND", "INTEREST", "INCOME"):
            continue
        sym = a.get("symbol") or {}
        tk = ((sym.get("symbol") if isinstance(sym, dict) else sym) or "").upper()
        d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
        amt = float(a.get("amount") or 0); un = float(a.get("units") or 0)
        k = (d, ty, tk, round(un, 4), round(amt, 2))
        if not d or not amt or k in seen:
            continue
        seen.add(k); evs.append((d, amt))
    bal = {}; run = 0.0
    for d, amt in sorted(evs):
        run += amt; bal[d] = round(run, 2)
    return bal

def _note_series(rows):
    """Structured notes / bonds (CUSIP-named) by date -> running face value from the trade feed."""
    seen = set(); evs = []
    for a in rows:
        ty = (a.get("type") or "").upper()
        if ty not in ("BUY", "SELL"):
            continue
        sym = a.get("symbol") or {}
        tk = ((sym.get("symbol") if isinstance(sym, dict) else sym) or "").upper()
        d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
        amt = abs(float(a.get("amount") or 0))
        k = (d, ty, tk, round(amt, 2))
        if not tk or not any(c.isdigit() for c in tk) or not d or not amt or k in seen:
            continue
        seen.add(k); evs.append((d, amt if ty == "BUY" else -amt))
    bal = {}; run = 0.0
    for d, amt in sorted(evs):
        run += amt; bal[d] = round(run, 2)
    return bal

def _equity_series(rows, skip=()):
    """Rebuild the equity book from trade activities and mark it to daily closes.
    Returns {date: {hv: market value, hpl: cumulative P&L incl. realized + dividends}}
    on trading days. The feed lists some trades twice (second copy with price 0) —
    dedup on the stable fields. CUSIP-named instruments (MMF, notes) are not equities."""
    seen = set(); ev = {}; divs = {}
    for a in rows:
        ty = (a.get("type") or "").upper()
        if ty not in ("BUY", "SELL", "OTHER", "DIVIDEND"):
            continue
        sym = a.get("symbol") or {}
        tk = ((sym.get("symbol") if isinstance(sym, dict) else sym) or "").upper()
        if not tk or any(c.isdigit() for c in tk) or tk in skip:
            continue
        d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
        units = float(a.get("units") or 0); amt = float(a.get("amount") or 0)
        k = (d, ty, tk, round(units, 4), round(amt, 2))
        if not d or k in seen:
            continue
        seen.add(k)
        if ty == "DIVIDEND":
            divs[d] = divs.get(d, 0.0) + amt
        elif ty == "OTHER":
            if units:  # in-kind transfer = acquisition at that day's value
                ev.setdefault(d, []).append((tk, units, -(amt or units * float(a.get("price") or 0))))
        else:
            ev.setdefault(d, []).append((tk, units, amt))
    if not ev:
        return {}
    import yfinance as yf
    tks = sorted({t for evs in ev.values() for t, _, _ in evs})
    _up("yahoo:pfhistory")
    px = yf.download(tks, start=min(ev), auto_adjust=False, progress=False)["Close"]
    if not hasattr(px, "columns"):
        px = px.to_frame(name=tks[0])
    px = px.ffill()
    shares = {t: 0.0 for t in tks}; cash = 0.0; div = 0.0
    evd = sorted(ev); dvd = sorted(divs); i = j = 0
    out = {}
    for ts in px.index:
        d = ts.strftime("%Y-%m-%d")
        while i < len(evd) and evd[i] <= d:
            for tk, du, dc in ev[evd[i]]:
                shares[tk] += du; cash += dc
            i += 1
        while j < len(dvd) and dvd[j] <= d:
            div += divs[dvd[j]]; j += 1
        row = px.loc[ts]
        mv = sum(shares[t] * float(row[t]) for t in tks if shares[t] and row[t] == row[t])
        out[d] = {"hv": round(mv, 2), "hpl": round(mv + cash + div, 2)}
    return out

@app.route("/api/lifetime")
def api_lifetime():
    """Net external deposits (cash flows + in-kind transfers) for lifetime P&L.
    Interest/dividends/normal INCOME stay OUT of flows — they ARE the P&L.
    Broker quirks handled: mislabeled big INCOME transfers, double-reported rows.
    account.json 'prior_net_deposits' covers anything predating the activity feed."""
    st = _st()
    if not st:
        return jsonify({})
    def fetch():
        try:
            rows = _st_activities(st)
            seen, flows, earliest, detail = set(), 0.0, None, []
            for a in sorted(rows, key=lambda x: str(x.get("trade_date") or x.get("settlement_date") or "")):
                d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
                typ = (a.get("type") or "").upper()
                amt = float(a.get("amount") or 0)
                desc = (a.get("description") or "").upper()
                earliest = earliest or d
                key = (d, typ, round(amt, 2))
                if key in seen:  # double-reported
                    continue
                seen.add(key)
                is_flow = (typ in ("CONTRIBUTION", "WITHDRAWAL")
                           or (typ == "OTHER" and amt and any(k in desc for k in ("MORGAN STANLEY", "INCOMING", "FUNDING", "TRANSFER")))
                           or (typ == "INCOME" and (abs(amt) >= 20000 or "TRANSFER" in desc or "FUNDING" in desc)))
                if is_flow and amt:
                    flows += amt
                    detail.append({"date": d, "type": typ, "amount": amt, "desc": (a.get("description") or "")[:70]})
            prior = read_account().get("prior_net_deposits", 0)
            return {"net_deposits": round(flows + prior, 2), "from_activity": round(flows, 2),
                    "prior": prior, "since": earliest, "flows": detail}
        except Exception:
            return _FailedDict()
    return jsonify(cached(f"lifetime:{_book()}", _ttl(LIVE["slow_s"]), fetch))

@app.route("/api/pfhistory")
def api_pfhist():
    st = _st()
    def fetch():
        if st:
            try:
                rows = _st_activities(st)
                # The broker's own balance-history series is unusable here: it flips the money-market
                # fund in and out on alternating days (a ±$278k square wave — 366 of 393 day-over-day
                # moves exceed $200k) and prints negative on 175 of 393 days. Rebuild the account
                # instead from the trade feed, which is exact: equities marked to daily closes, plus
                # the money-market balance, plus the note.
                acct = read_account()
                ce_tks, mmf_bal = _cash_equiv(rows)
                hs = _equity_series(rows, skip=ce_tks)
                cash_bal = _cash_ledger(rows)
                if not hs:
                    # A book with no equity trades yet (DIP Venture, funded 2026-09-10 and
                    # all cash): the chart is the cash + sweep ledger on weekdays from the
                    # first flow to today, equity value zero. Without this the page showed
                    # an empty chart for a $1.3M account.
                    first = min(list(cash_bal) + list(mmf_bal) + [dt.date.today().isoformat()])
                    d0 = dt.date.fromisoformat(first[:10]); today = dt.date.today()
                    hs = {}
                    while d0 <= today:
                        if d0.weekday() < 5:
                            hs[d0.isoformat()] = {"hv": 0.0, "hpl": 0.0}
                        d0 += dt.timedelta(days=1)
                    if not hs:
                        raise ValueError("no series")
                note_bal = _note_series(rows)  # face value; carried at the broker's live mark
                face = note_bal[max(note_bal)] if note_bal else 0.0
                nscale = (float(acct.get("structured_note") or 0) / face) if face else 1.0
                flows = _pf_flows(rows)
                fd = sorted(flows); md = sorted(mmf_bal); nd = sorted(note_bal); cd = sorted(cash_bal)
                pts = []; cum = mm = nv = cb = 0.0; i = j = k = n = 0
                for d in sorted(hs):
                    while i < len(fd) and fd[i] <= d:
                        cum += flows[fd[i]]; i += 1
                    while j < len(md) and md[j] <= d:
                        mm = mmf_bal[md[j]]; j += 1
                    while k < len(nd) and nd[k] <= d:
                        nv = note_bal[nd[k]]; k += 1
                    while n < len(cd) and cd[n] <= d:
                        cb = cash_bal[cd[n]]; n += 1
                    h = hs[d]
                    val = h["hv"] + mm + nv * nscale + cb
                    pts.append({"date": d, "value": round(val, 2), "pl": round(val - cum, 2), **h})
                # The broker's total is the authority on what the account is worth; our
                # rebuild from the trade feed misses any un-itemized sweep balance (see
                # account.json mmf_residual). Anchor the level to the broker when the gap
                # is small — shape from the ledger, level from the broker — so the chart
                # ends exactly on the Account-value KPI instead of ~1% below it.
                # `pl` deliberately does NOT move: that money arrived as a deposit and the
                # flow ledger already counted it.
                tv = float(acct.get("total_value") or 0)
                if pts and tv:
                    gap = tv - pts[-1]["value"]
                    if abs(gap) <= 0.02 * tv:
                        for p in pts:
                            p["value"] = round(p["value"] + gap, 2)
                return pts
            except Exception:
                pass
        return _bjson("pf_history.json", [])  # snapshot fallback (grows on each sync)
    return jsonify(cached(f"pfhist:{_book()}", _ttl(LIVE["chart_s"]), fetch))

@app.route("/api/research", methods=["POST"])
def api_research():
    tk = request.args.get("ticker", "").upper().strip()
    if not tk:
        return jsonify({"msg": "no ticker"})
    r = runner.launch_research(tk)
    if not r.get("ok"):
        return jsonify({"msg": r.get("msg", "launch failed")})
    if r.get("queued"):
        return jsonify({"ok": True, "queued": True, "msg": f"Research on {tk} {r.get('msg', 'queued')} — one Claude session at a time, in the next open window."})
    return jsonify({"ok": True, "msg": f"Research on {tk} started — a full Claude session is running in the background (an hour or two). It'll appear under Research when done."})

# The teardown is specified as an ordered set of deliverables (runner.research_prompt
# / RUNBOOK.md), so progress is a fact on disk, not a guess. The old bar tailed the last
# log line — prose that says nothing about how far along the run is, and that keeps
# scrolling long after the run has died.
RESEARCH_STEPS = [
    ("Evidence pack", "research/_evidence/INDEX.md"),
    ("Governance & ownership", "research/governance-insiders-ownership.md"),
    ("Product & business", "research/product-business.md"),
    ("Competitive & market", "research/competitive-market.md"),
    ("IP, legal & moat", "research/ip-legal-moat.md"),
    ("Financials & capital structure", "research/financials-capital-structure.md"),
    ("Adversarial refuter", "research/adversarial-review.md"),
    ("Valuation", "analysis/valuation.md"),
    ("Dossier", "analysis/soft-research-dossier.md"),
    ("Trade playbook", "analysis/trade-playbook.md"),
    ("Final report", "analysis/FINAL-REPORT.md"),
    ("Lenses (dashboard verdict)", "analysis/lenses.json"),
    ("Thesis card", "analysis/card.json"),
]

def _research_progress(tk):
    """Which deliverables exist. Pillar filenames vary by run, so a missing exact match
    falls back to any .md in research/ that is not a pillar we already matched."""
    cf = company_folder(tk)
    if not cf:
        return {"steps": [], "done": 0, "total": len(RESEARCH_STEPS), "pct": 0}
    steps = []
    for label, rel in RESEARCH_STEPS:
        p = cf / rel
        ok = p.exists() and (p.stat().st_size > 200 or p.is_dir())
        steps.append({"label": label, "file": rel, "done": ok})
    ndone = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": ndone, "total": len(steps),
            "pct": round(100 * ndone / len(steps))}

def _run_status(logf, err_hint=""):
    """Shared poll logic: (status, msg) from a runner log file."""
    if not logf.exists():
        return "idle", ""
    err = runner.log_error(logf)
    if err:
        return "error", err
    # Liveness comes from the process, not from how recently the log was touched. A
    # dead run used to keep saying "running" for an hour and then quietly become
    # "idle" — which the UI hides, so a failure disappeared instead of reporting.
    state = runner.run_state(logf)
    if state == "exited":
        return "stopped", ""
    if state == "unknown" and time.time() - logf.stat().st_mtime > 1800:
        return "stopped", ""  # pre-pid-tracking run with a long-cold log
    try:
        lines = [l for l in logf.read_text(errors="ignore").strip().splitlines() if l.strip()]
        return "running", (lines[-1][:140] if lines else "starting…")
    except Exception:
        return "running", "running…"

def _queued(key):
    """ETA line for a job waiting in the Claude queue (claudeq), or '' when not queued."""
    try:
        import claudeq
        j = claudeq.pending_for(key)
        return claudeq.eta_msg(j) if j else ""
    except Exception:
        return ""

@app.route("/api/research/status")
def research_status():
    tk = request.args.get("ticker", "").upper().strip()
    runner.reap()          # before any early return — a finished run must not stay <defunct>
    cf = company_folder(tk)
    prog = _research_progress(tk)
    if cf and (cf / "analysis" / "FINAL-REPORT.md").exists():
        return jsonify({"done": True, "status": "done", "progress": prog,
                        "link": str((cf / "analysis" / "FINAL-REPORT.md").relative_to(ROOT))})
    q = _queued(f"research:{tk}")
    if q:
        return jsonify({"done": False, "status": "queued", "msg": q, "progress": prog})
    logf = runner.research_log(tk)
    if not logf.exists():  # legacy location
        logf = ROOT / f"_engine/candidate-boards/_research_{tk}.log"
    status, msg = _run_status(logf)
    # A run that stopped without a FINAL-REPORT did NOT finish, and saying so is the
    # whole point — it is recoverable work sitting on disk, not an empty slate.
    if status == "stopped" and prog["done"]:
        missing = [s["label"] for s in prog["steps"] if not s["done"]]
        status, msg = "incomplete", (f"Stopped with {prog['done']}/{prog['total']} deliverables. "
                                     f"Missing: {', '.join(missing[:4])}"
                                     + (f" +{len(missing) - 4} more" if len(missing) > 4 else ""))
    elif status == "stopped":
        status = "idle"
    return jsonify({"done": False, "status": status, "msg": msg, "progress": prog})

@app.route("/api/research/update", methods=["POST"])
def api_research_update():
    tk = request.args.get("ticker", "").upper().strip()
    if not tk:
        return jsonify({"msg": "no ticker"})
    r = runner.launch_update(tk)
    if not r.get("ok"):
        return jsonify({"msg": r.get("msg", "launch failed")})
    if r.get("queued"):
        return jsonify({"ok": True, "queued": True, "msg": f"Update for {tk} {r.get('msg', 'queued')}."})
    return jsonify({"ok": True, "msg": f"Updating {tk} research — reading everything new since the report date (a few minutes)."})

@app.route("/api/research/update/status")
def research_update_status():
    tk = request.args.get("ticker", "").upper().strip()
    cf = company_folder(tk)
    today = dt.date.today().isoformat()
    for cand in ([cf / "analysis" / "updates" / f"update-{today}.md", cf / "analysis" / f"update-{today}.md"] if cf else []):
        if cand.exists():
            return jsonify({"done": True, "status": "done", "link": str(cand.relative_to(ROOT))})
    q = _queued(f"update:{tk}")
    if q:
        return jsonify({"done": False, "status": "queued", "msg": q})
    status, msg = _run_status(runner.update_log(tk))
    return jsonify({"done": False, "status": status, "msg": msg})

@app.route("/api/recommend", methods=["POST"])
def api_recommend():
    r = runner.launch_rec()
    if not r.get("ok"):
        return jsonify({"msg": r.get("msg", "launch failed")})
    if r.get("queued"):
        return jsonify({"ok": True, "queued": True, "msg": f"Recommendation {r.get('msg', 'queued')}."})
    return jsonify({"ok": True, "msg": "Generating today's recommendation — a Claude session is reading the portfolio (a few minutes)."})

@app.route("/api/recommend/status")
def recommend_status():
    p = runner.rec_path()
    if p.exists():
        return jsonify({"done": True, "status": "done", "link": str(p.relative_to(ROOT))})
    q = _queued("rec")
    if q:
        return jsonify({"done": False, "status": "queued", "msg": q})
    status, msg = _run_status(runner.rec_log())
    return jsonify({"done": False, "status": status, "msg": msg})

@app.route("/api/board/refresh", methods=["POST"])
def board_refresh():
    try:
        py = str(ROOT / "_engine/.venv/bin/python")
        cmd = f"{py} scanners/insider_cluster.py --days 30 --min-buyers 2 && {py} scanners/enrich_board.py --top 55 --min-cap 5e7"
        log = open(str(ROOT / "_engine/candidate-boards/_board_refresh.log"), "w")
        subprocess.Popen(["bash", "-lc", cmd], cwd=str(ROOT / "_engine"), stdout=log, stderr=log, start_new_session=True)
        return jsonify({"ok": True, "msg": "Candidate board refresh started (~10-15 min). Reload the board when it's done."})
    except Exception as e:
        return jsonify({"msg": "refresh error: " + str(e)[:120]})

@app.route("/api/external")
def api_external():
    return jsonify(read_external())

FEEDBACK = ROOT / "_engine" / "recommendations" / "feedback.json"

@app.route("/api/feedback", methods=["GET", "POST"])
def api_feedback():
    try:
        items = json.loads(FEEDBACK.read_text()) if FEEDBACK.exists() else []
    except Exception:
        items = []
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        msg = (b.get("msg") or "").strip()
        if b.get("action") == "remove":  # POST-only since PLUMB-8 (2026-09-22) — a GET just lists
            items = [e for e in items if str(e.get("id")) != str(b.get("id", ""))]
            FEEDBACK.write_text(json.dumps(items, indent=2))
        elif msg:
            items.append({"id": int(time.time() * 1000), "date": dt.date.today().isoformat(), "msg": msg[:1000]})
            FEEDBACK.write_text(json.dumps(items, indent=2))
    return jsonify(items)

@app.route("/api/research/archive", methods=["POST"])
def api_archive():
    tk = request.args.get("ticker", "").upper().strip()
    cf = company_folder(tk)
    if not cf:
        return jsonify({"msg": f"No research folder found for {tk}."})
    if (read_positions().get(tk, {}).get("shares") or 0) > 0:
        return jsonify({"msg": f"{tk} is a live holding — not archiving its research."})
    import shutil
    dest = ROOT / "_archive"
    dest.mkdir(exist_ok=True)
    tgt = dest / cf.name
    if tgt.exists():
        tgt = dest / f"{cf.name}_{int(time.time())}"
    shutil.move(str(cf), str(tgt))
    return jsonify({"ok": True, "msg": f"{cf.name} moved to _archive/ (restore by moving it back)."})

@app.route("/api/lots")
def api_lots():
    tk = request.args.get("ticker", "").upper().strip()
    st = _st()
    if not (st and tk):
        return jsonify([])
    def fetch():
        try:
            rows = _st_activities(st)
            best = {}  # AggregatorA double-reports some fills (with/without price) — keep the priced row
            for a in rows:
                sym = a.get("symbol") or {}
                s = (sym.get("symbol") if isinstance(sym, dict) else sym) or ""
                typ = (a.get("type") or "").upper()
                if s.upper() != tk:
                    continue
                units = float(a.get("units") or 0)
                if typ in ("REINVEST", "REI"):
                    typ = "REINVEST"
                elif typ not in ("BUY", "SELL"):
                    # positions can arrive via broker transfer (e.g. AMZN from Morgan Stanley):
                    # type OTHER/TRANSFER with units but no price — still a real tranche
                    if units and typ in ("OTHER", "TRANSFER"):
                        typ = "TRANSFER"
                    else:
                        continue
                d = str(a.get("trade_date") or a.get("settlement_date") or "")[:10]
                price = float(a.get("price") or 0); amt = float(a.get("amount") or 0)
                if not price and units and amt:
                    price = round(abs(amt) / abs(units), 4)  # implied from consideration
                key = (d, round(units, 4), typ)
                if key not in best or (best[key]["price"] == 0 and price > 0):
                    best[key] = {"date": d, "type": typ, "units": units, "price": price, "amount": a.get("amount")}
            return sorted(best.values(), key=lambda x: x["date"])
        except Exception:
            return _Failed()
    return jsonify(cached(f"lots:{_book()}:{tk}", _ttl(LIVE["activity_s"]), fetch))

@app.route("/api/calendar")
def api_calendar():
    def fetch():
        pos = read_positions(); wl = watchlist()
        held = [t for t, m in pos.items() if (m.get("shares") or 0) > 0]
        out = []; undated = []
        for tk in dict.fromkeys(held + wl):
            d = next_earnings(tk)
            if d:
                out.append({"date": d, "tk": tk, "kind": "earnings",
                            "label": "Earnings", "held": tk in held})
            else:
                undated.append(tk)
        for e in _json("calendar.json", []):
            e = dict(e); e.setdefault("held", bool(e.get("tk") and e["tk"] in held))
            out.append(e)
        today = dt.date.today().isoformat()
        out = [e for e in out if e.get("date") and e["date"] >= today]
        out.sort(key=lambda x: x["date"])
        # a quiet horizon is a real answer, not a stale widget — say which names simply
        # have no date on the wire so an empty-looking calendar reads as "nothing due".
        return {"events": out[:24], "undated": undated, "as_of": today}
    return jsonify(cached("cal", LIVE["slow_s"], fetch))   # local: re-reads next_earnings()' cache

# ---------- David's journal: dated notes + decision log (the learning loop) ----------
JDIR = ROOT / "_engine" / "journal"

def _jfile(name):
    try:
        return json.loads((JDIR / name).read_text())
    except Exception:
        return []

def _jwrite(name, items):
    JDIR.mkdir(exist_ok=True)
    (JDIR / name).write_text(json.dumps(items, indent=2))

@app.route("/api/journal/notes", methods=["GET", "POST"])
def api_jnotes():
    items = _jfile("notes.json")
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        msg = (b.get("msg") or "").strip()
        if b.get("action") == "remove":  # POST-only since PLUMB-8 (2026-09-22) — a GET just lists
            items = [e for e in items if str(e.get("id")) != str(b.get("id", ""))]
            _jwrite("notes.json", items)
        elif msg:
            items.append({"id": int(time.time() * 1000), "date": dt.date.today().isoformat(),
                          "tk": (b.get("tk") or "").upper().strip()[:8], "msg": msg[:4000]})
            _jwrite("notes.json", items)
    return jsonify(items)

@app.route("/api/journal/decisions", methods=["GET", "POST"])
def api_jdecisions():
    items = _jfile("decisions.json")
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        if b.get("close"):  # resolve an open decision
            for e in items:
                if str(e.get("id")) == str(b.get("id")):
                    e["status"] = "closed"; e["closed"] = dt.date.today().isoformat()
                    e["outcome"] = (b.get("outcome") or "")[:1000]
                    e["process_score"] = b.get("score")
            _jwrite("decisions.json", items)
        elif (b.get("thesis") or "").strip():
            items.append({"id": int(time.time() * 1000), "date": b.get("date") or dt.date.today().isoformat(),
                          "tk": (b.get("tk") or "").upper().strip()[:8], "action": (b.get("action") or "note")[:12],
                          "qty": b.get("qty"), "price": b.get("price"),
                          "thesis": b.get("thesis", "")[:2000], "expect": b.get("expect", "")[:1000],
                          "kill": b.get("kill", "")[:1000], "source": b.get("source", "")[:80],
                          "status": "open"})
            _jwrite("decisions.json", items)
    return jsonify(items)

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip().upper()
    if not q or len(q) > 40:
        return jsonify([])
    names = ticker_names(); wl = set(watchlist()); held = held_all()   # owned in ANY book, not the one on screen
    researched = {ticker_of(c) for c in companies()}
    scored = []
    for tk, nm in names.items():
        if tk.startswith(q):
            scored.append((0 if tk == q else 1, tk))
        elif len(q) >= 3 and q.lower() in nm.lower():
            scored.append((2, tk))
    scored.sort(key=lambda x: (x[0], len(x[1]), x[1]))
    out = [{"tk": tk, "name": names.get(tk, ""),
            "held": tk in held,
            "watch": tk in wl, "research": tk in researched}
           for _, tk in scored[:8]]
    return jsonify(out)

CSS = """
:root{--bg:#f4f6f8;--panel:#fff;--fg:#161b22;--mut:#4b5563;--fade:#7c8794;--line:#e3e7ed;
--acc:#2563eb;--accbg:#e9f0fe;--neg:#cf4444;--pos:#0e9160;--red:#cf4444;--yel:#b07d0a;--grn:#0e9160;
--shadow:0 1px 2px rgba(16,24,40,.05),0 8px 24px rgba(16,24,40,.05)}
@media(prefers-color-scheme:dark){:root{--bg:#0c0e11;--panel:#15181d;--fg:#e8eaee;--mut:#a8b1bc;
--fade:#828d9a;--line:#262b32;--acc:#5e97f5;--accbg:#16263e;--neg:#e0685c;--pos:#3dbd85;--red:#e0685c;--yel:#dfa93c;--grn:#3dbd85;
--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 26px rgba(0,0,0,.32)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);display:flex;
font:15.5px/1.6 Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-feature-settings:'tnum' 1,'cv11' 1;-webkit-font-smoothing:antialiased}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
/* The click handlers run in under 6ms — everything that used to read as lag was the
   animation on top of them (David 2026-08-13: "still the slightest delay"). Selection
   state is now instant everywhere; only the page-content swap keeps a token fade, at
   .07s opacity-only. The 3px slide is gone: sliding text is what read as "settling". */
@keyframes fade{from{opacity:.55}to{opacity:1}}
#main{animation:fade .07s linear}
button,a,.tab,.rbtn,.fbtn,.pfseg,.chip,.dt tbody tr{touch-action:manipulation}
.side{width:272px;flex-shrink:0;height:100vh;position:sticky;top:0;overflow:auto;
background:var(--panel);border-right:1px solid var(--line);padding:0 12px 40px}
.sidetop{padding:16px 8px 10px}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;font-size:15.5px;letter-spacing:-.01em;color:var(--fg)}
.brand:hover{text-decoration:none}.logo{width:20px;height:20px;flex-shrink:0}.logo rect{fill:var(--acc)}
.ic{width:15px;height:15px;flex-shrink:0}
.search{position:relative;display:flex;align-items:center;margin:2px 4px 10px}
.search>.ic{position:absolute;left:11px;width:14px;height:14px;color:var(--fade);pointer-events:none}
.search input{width:100%;padding:8px 32px 8px 33px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--fg);font-size:13.5px;font-family:inherit;transition:border-color .12s}
.search input:focus{outline:none;border-color:var(--acc)}
.skey{position:absolute;right:9px;font:11px/1.4 ui-monospace,monospace;border:1px solid var(--line);border-radius:4px;
padding:0 5px;color:var(--fade);background:var(--panel);pointer-events:none}
.sresults{position:absolute;top:calc(100% + 6px);left:0;right:0;background:var(--panel);border:1px solid var(--line);
border-radius:11px;box-shadow:var(--shadow);z-index:45;overflow:hidden;display:none}
.sresults.show{display:block}
.sri{display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;font-size:13px}
.sri.on{background:var(--accbg)}
.srtk{font-weight:600;min-width:46px}
.srname{flex:1;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srtag{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);border-radius:5px;padding:1px 6px;color:var(--mut);white-space:nowrap}
.srtag.h{color:var(--pos);border-color:var(--pos)}.srtag.r{color:var(--acc);border-color:var(--acc)}
details>summary{list-style:none;cursor:pointer;font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;font-weight:600;
color:var(--mut);padding:13px 8px 5px;user-select:none;display:flex;align-items:center;gap:6px}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:'';border:solid var(--fade);border-width:0 1.5px 1.5px 0;padding:2.5px;
transform:rotate(-45deg);transition:transform .15s}details[open]>summary::before{transform:rotate(45deg)}
.leaf{display:flex;align-items:center;gap:9px;padding:7px 10px;margin:1px 0;border-radius:7px;color:var(--fg);
font-size:14px;cursor:pointer;transition:background .1s;min-width:0}
.leaf:hover{background:var(--bg);text-decoration:none}.leaf.on{background:var(--accbg);color:var(--acc);font-weight:500}
.leaf .ic{color:var(--fade)}.leaf.on .ic{color:var(--acc)}
.leaf.action{color:var(--acc);font-weight:500}.leaf.action .ic{color:var(--acc)}
.leaf>span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lheld{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--pos);
background:color-mix(in srgb,var(--pos) 10%,var(--panel));border:1px solid color-mix(in srgb,var(--pos) 45%,var(--line));
border-radius:5px;padding:1px 6px;flex-shrink:0}
.vdot{width:8px;height:8px;border-radius:50%;background:var(--line);flex-shrink:0}
.vdot.buy{background:var(--grn)}.vdot.sell{background:var(--red)}.vdot.warn{background:var(--yel)}.vdot.neu{background:var(--fade)}
/* a deep-linked item (/dip/options#aci) scrolls to just under the sticky seg bar, not beneath it */
#main [id]{scroll-margin-top:76px}
.pfnav{position:sticky;top:0;z-index:30;background:var(--bg);display:flex;gap:4px;
padding:10px 0 10px;margin:0 0 16px;border-bottom:1px solid var(--line)}
.pfback{flex:0 0 auto!important;padding:11px 14px;font-size:16px}
.pfseg{flex:1;text-align:center;padding:11px 2px;border-radius:10px;font-size:13.5px;
font-weight:600;color:var(--mut);min-height:44px;display:flex;align-items:center;
justify-content:center;text-decoration:none}
.pfseg:hover{background:var(--panel);text-decoration:none}
.pfseg.on{background:var(--accbg);color:var(--acc)}
.sempty{font-size:13px;color:var(--mut);padding:4px 10px 8px;line-height:1.5}
/* (the old sidebar's .tkleaf/.codet/.srm/.lchg rules went with the tree, 2026-09-22) */
/* ---- sidebar, 2026-09-22 redesign: five destinations, then Holdings (HBS grouped-list idiom
   in this shell's tokens: 9px rows, one tint for what you can tap, sentence-case group labels,
   tabular numbers). Exactly one .ni is .on — setActive()/nav_key() decide which. ---- */
.snav{display:flex;flex-direction:column;gap:2px;margin:2px 0 4px}
.snav .leaf,.sfoot .leaf{min-height:40px;padding:0 10px;margin:0;border-radius:9px;gap:11px;font-size:15px;font-weight:500}
.snav .leaf .ic{width:18px;height:18px;color:var(--acc)}
.ni.on>.leaf{background:var(--accbg);color:var(--acc);font-weight:600}
.snav .leaf .lheld{margin-left:auto;text-transform:none;letter-spacing:0;font-size:12px;color:var(--yel);
background:color-mix(in srgb,var(--yel) 12%,var(--panel));border-color:color-mix(in srgb,var(--yel) 40%,var(--line))}
.hsec{margin-top:16px}
.hsec-h{font-size:15px;font-weight:600;color:var(--fg);padding:4px 10px 0;display:flex;align-items:baseline;gap:6px}
.hgrp{display:flex;align-items:baseline;gap:8px;padding:12px 10px 3px;font-size:13px;font-weight:600;color:var(--mut)}
.hday{margin-left:auto;font-variant-numeric:tabular-nums}.hday.up{color:var(--pos)}.hday.down{color:var(--neg)}
.leaf.hrow{min-height:44px;padding:4px 10px;gap:10px;margin:0;border-radius:9px}
.leaf.hrow>span{display:flex;flex-direction:column;line-height:1.25;min-width:0;overflow:visible}
.hrow .hr{margin-left:auto;align-items:flex-end;text-align:right}
.hrow .htk{font-weight:600;font-size:14px;color:var(--fg)}
.hrow .hv{font-size:12px;color:var(--fade);font-variant-numeric:tabular-nums}
.hrow .hd{font-size:13.5px;font-weight:600;color:var(--mut);font-variant-numeric:tabular-nums}
.hrow .hp{font-size:12px;color:var(--fade);font-variant-numeric:tabular-nums}
.hrow .hd.up,.hrow .hp.up{color:var(--pos)}.hrow .hd.down,.hrow .hp.down{color:var(--neg)}
.hrow.priv .hd{font-size:12px;font-weight:500;color:var(--fade)}
.leaf.hrow.cur{background:var(--bg);box-shadow:inset 3px 0 0 var(--acc)}
.sfoot{margin-top:18px;padding-top:8px;border-top:1px solid var(--line)}
.sfoot .leaf{font-size:13px;color:var(--fade);font-weight:500}.sfoot .leaf .ic{color:var(--fade)}
@media(max-width:820px){.snav .leaf,.sfoot .leaf{min-height:44px}form.search input,form.addbar input{font-size:16px}
.mobilebar .burger{width:44px;height:44px}.search .skey{display:none}
form.search input,form.addbar input,form.addbar button,.headactions .connectbtn{min-height:44px}
.fhead .ftk{padding:12px 0;margin:-12px 0}}
/* ---- /research (Watchlist · Companies · Boards) ---- */
.wfoot{padding:12px 16px;border-top:1px solid var(--line);font-size:13px;color:var(--mut);display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.wchip{display:inline-flex;align-items:center;gap:4px;font-weight:600}
.dt .rmc .wbtn{height:32px;padding:0 11px;font-size:12.5px}
.rgrp-h{font-size:13px;font-weight:600;color:var(--mut);margin:22px 4px 8px}
.rco-list{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.rco{padding:10px 14px 12px;border-top:1px solid var(--line);min-width:0}.rco:first-child{border-top:none}
.rco-top{display:flex;align-items:center;gap:9px;min-height:36px;min-width:0}
.rco-nm{font-weight:600;font-size:15.5px;color:var(--fg);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rco-tk{font-size:12.5px;font-weight:600;color:var(--fade);flex-shrink:0}
.rco-x{margin-left:auto}
.rco-v{font-size:13px;color:var(--mut);margin:0 0 6px 17px}
.rco-links{display:flex;flex-wrap:wrap;gap:6px;margin-left:17px}
.rlink{display:inline-flex;align-items:center;min-height:34px;padding:0 12px;border-radius:9px;background:var(--bg);
border:1px solid var(--line);font-size:13.5px;font-weight:500;color:var(--acc)}
.rlink:hover{border-color:var(--acc);text-decoration:none}
@media(max-width:820px){.rlink{min-height:44px}.rco .rm,.wchip .rm,.dt .rmc .rm{width:44px;height:44px}.dt .rmc .wbtn{height:44px}
.rco-top{min-height:44px}.rco-nm{padding:10px 0;margin:-10px 0}.wchip a{padding:12px 2px;margin:-12px 0}.fbtn{min-height:44px}
.rco-v,.rco-links{margin-left:0}}
@media(max-width:640px){#nametable .c-mid,#nametable .tag,.dt .rmc .wl{display:none}.dt .rmc .wbtn{width:44px;padding:0}
#nametable th,#nametable td{padding-left:8px;padding-right:8px}}
.dt .rmc .wbtn.on{background:var(--accbg);border-color:transparent;color:var(--acc)}
#nametable th.rmc{font-size:12px;text-align:center}
/* WIDTH SYSTEM (2026-08-31, David: "nothing hard coded — adjusting accordingly,
   sizing flexible based on screen size"). The frame is FLUID: no pixel cap, padding
   scales with the viewport. Each content type carries its own INTRINSIC constraint
   instead — prose measures in ch (scales with its own font), card grids auto-fit
   (column count follows real container width), rails clamp(). Add a page by giving
   its content an intrinsic size; never by capping the frame. */
main{flex:1;min-width:0;width:100%;margin:0 auto;
padding:clamp(20px,3vw,64px) clamp(16px,4vw,96px) 110px}
/* fluid type: grows with the screen (15px phone -> ~19px on the 31"), and because the
   article measure below is in ch, the text column widens WITH its font — continuously,
   no breakpoints */
article{max-width:min(100%,100ch);font-size:clamp(15px,.32vw + 11.5px,19px);line-height:1.7}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:22px;flex-wrap:wrap;margin-bottom:clamp(18px,2vw,30px)}
.pagehead h1{margin:0;font-size:clamp(26px,1.2vw + 20px,36px);letter-spacing:-.02em}.headactions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.connectbtn .ic,.btn .ic{width:14px;height:14px}
.kpirow{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:18px;margin-bottom:28px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:24px 26px;box-shadow:var(--shadow)}
.kpi.hero{grid-column:span 2}
.kk{font-size:13px;color:var(--mut);margin-bottom:12px;font-weight:500}.kk .ksub{font-weight:400;color:var(--fade)}
.kv{font-size:clamp(26px,1.3vw + 18px,38px);font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.025em;line-height:1.05}
.kpi.hero .kv{font-size:clamp(34px,2vw + 22px,52px)}.kv.up{color:var(--pos)}.kv.down{color:var(--neg)}
.homegrid{display:grid;grid-template-columns:minmax(0,1fr) clamp(320px,28vw,430px);gap:26px;align-items:start}
.homemain>.widget,.homerail>.widget{margin-bottom:20px}
.homemain>.widget:last-child,.homerail>.widget:last-child{margin-bottom:0}
.homemain{display:flex;flex-direction:column;gap:22px;min-width:0}
.homerail{position:sticky;top:24px;display:flex;flex-direction:column;gap:22px}
.widget{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:var(--shadow)}
.whead{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:18px 24px;border-bottom:1px solid var(--line)}
.wtitle{font-size:16.5px;font-weight:600}.wcount{font-size:13px;color:var(--mut);background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:2px 11px}
.wbody{padding:2px 0;overflow-x:auto;-webkit-overflow-scrolling:touch}.wbody.pad{padding:22px 24px}
.dt{width:100%;border-collapse:collapse;font-size:15px}
.dt th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:600;padding:13px 24px;border-bottom:1px solid var(--line)}
.dt td{padding:17px 24px;border-bottom:1px solid var(--line);vertical-align:middle}.dt tbody tr:last-child td{border-bottom:none}
.dt tbody tr{cursor:pointer;transition:background .1s}.dt tbody tr:hover td{background:var(--bg)}
.dt .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.dt .chg.up{color:var(--pos)}.dt .chg.down{color:var(--neg)}.dt .price{font-weight:600}
.dt .tklink{font-size:16px;font-weight:600}.dt .pill{font-size:12.5px;padding:3px 12px}
.dt .rmc{width:40px;text-align:center}.dt .cat{color:var(--mut);font-size:14px}
.pfempty2{padding:40px 24px;color:var(--mut);font-size:15.5px;text-align:center}
.subname{font-size:12.5px;color:var(--mut);font-weight:400;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
/* max-width, overflow and text-overflow do NOT apply to an inline non-replaced box, so the
   rule above only ever set nowrap on a <span class=subname> — long text then ran past its
   container instead of ellipsising (PE firm cards, 1,298px of strategy text in a 900px
   column, 2026-09-21). Inline ones wrap; the block ones keep the ellipsis. */
span.subname{white-space:normal;overflow:visible;max-width:none}
/* A grid/flex child defaults to min-width:auto and will not shrink below its content, which
   pushes it out of its own track on a narrow phone. These two are the home layout's columns.
   Long unbreakable strings (paths, tickers in <code>) break instead of overflowing. */
.homegrid>*,.homemain,.homerail,.whead>*{min-width:0}
/* a head whose controls outgrow the row wraps them under the title (the Portfolio chart's
   mode + range buttons slid over its title on phones) */
.whead.wrapx{flex-wrap:wrap}.whead.wrapx>.wtitle{flex-shrink:0}
/* the watchlist table sits in the half-width column beside Coming up on /research: size its
   columns to the card, not the window, so the secondary ones drop before it has to scroll */
.homemain{container-type:inline-size}
@container (max-width:720px){#nametable .c-wide,#nametable .tag,.dt .rmc .wl{display:none}
#nametable th,#nametable td{padding-left:12px;padding-right:12px}#nametable .pill{white-space:normal}#nametable .subname{max-width:140px}
.dt .rmc .wbtn{width:44px;padding:0}}
@container (max-width:520px){#nametable .c-mid{display:none}
/* a phone-width card: the Name cell gives way (width:100% + max-width:0 takes whatever is left
   and its sub-name ellipsises) so Price, Day and the Watch toggle always fit — the table was
   373px in a 356px card at 390 and 300 in 286 at 320, cutting the toggle off (2026-09-22 review).
   Not table-layout:fixed: Chrome still counts the display:none columns there and split the
   spare width between them and Name, leaving Name 28px. */
#nametable{width:100%}
#nametable th:first-child,#nametable td:first-child{width:100%;max-width:0;overflow:hidden;padding-left:12px}
#nametable .c-px,#nametable .c-d1,#nametable .rmc{white-space:nowrap}
#nametable th,#nametable td{padding-left:5px;padding-right:5px}
#nametable .subname{max-width:100%}#nametable .tklink{display:inline-block;padding:10px 0;margin:-10px 0}}
code{overflow-wrap:anywhere}
.dt .pnl.up{color:var(--pos)}.dt .pnl.down{color:var(--neg)}.dt .pnl{font-weight:600}.dt .pnl .pct{font-weight:400;opacity:.8}
.arow{display:flex;justify-content:space-between;align-items:baseline;padding:11px 0;border-bottom:1px solid var(--line);font-size:15px}
.arow:last-child{border-bottom:none}.arow>span:first-child{color:var(--mut)}
.arow.total{font-weight:600;border-top:2px solid var(--line);border-bottom:none;margin-top:4px;padding-top:13px}
.arow.total>span:first-child{color:var(--fg)}.amono{font-variant-numeric:tabular-nums;font-weight:500}
@media(max-width:1060px){.homegrid{grid-template-columns:1fr}.homerail{position:static}}
/* ---- bench table (/research) + the same return cells in the Book's holdings ---- */
#nametable .num{font-weight:500}
.dt .p-m1.up,.dt .p-m6.up,.dt .p-ytd.up,.dt .p-d1.up{color:var(--pos)}
.dt .p-m1.down,.dt .p-m6.down,.dt .p-ytd.down,.dt .p-d1.down{color:var(--neg)}
#nametable .p-price{font-weight:600}
.tag{font-size:10.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin-left:8px;
padding:2px 7px;border-radius:5px;border:1px solid var(--line);color:var(--mut);vertical-align:middle}
.tag.held{border-color:color-mix(in srgb,var(--acc) 45%,var(--line));background:var(--accbg);color:var(--acc)}
.tag.res{border-style:dashed}
.doclink{font-size:13px}
/* phones: keep the columns that answer "how is it doing", drop the rest (never shrink) */
@media(max-width:900px){.dt .c-wide{display:none}}
@media(max-width:700px){.subname{max-width:120px}}
.hero h1{font-size:30px;font-weight:600;margin:0 0 .15em;letter-spacing:-.02em}
h1,h1.doctitle{font-size:clamp(23px,1vw + 18px,30px);font-weight:600;margin:.1em 0 .4em;letter-spacing:-.015em}
.tksym{font-size:16px;color:var(--mut);font-weight:500;margin-left:4px}
.crumb{font-size:12px;color:var(--fade);font-family:ui-monospace,monospace;margin-bottom:4px}
.muted,.fdesc.muted{color:var(--mut)}.hint{font-size:11.5px;color:var(--fade);text-transform:none;letter-spacing:0;font-weight:400}
.summary{font-size:clamp(15px,.25vw + 14px,17px);line-height:1.68;color:var(--mut);max-width:none;margin:.4em 0 .8em}
.sec{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--fade);margin:1.7em 0 .7em;font-weight:600}
h2{font-size:20px;font-weight:600;margin:1.7em 0 .5em;padding-bottom:.25em;border-bottom:1px solid var(--line)}
h3{font-size:16px;font-weight:600;margin:1.4em 0 .4em}
.btn,.wbtn,.rbtn2,.connectbtn,.addbar button{display:inline-flex;align-items:center;justify-content:center;gap:7px;
height:36px;padding:0 15px;border-radius:9px;border:1px solid var(--line);background:var(--panel);color:var(--fg);
font-family:inherit;font-size:13.5px;font-weight:500;line-height:1;cursor:pointer;white-space:nowrap;
transition:border-color .12s,color .12s,background .12s;text-decoration:none}
.btn:hover,.wbtn:hover,.connectbtn:hover{border-color:var(--acc);color:var(--acc);text-decoration:none}
.btn.primary,.addbar button{background:var(--acc);border-color:var(--acc);color:#fff}
.btn.primary:hover,.addbar button:hover{opacity:.92;color:#fff}
.rbtn2{color:var(--acc);border-color:color-mix(in srgb,var(--acc) 40%,var(--line));background:var(--accbg)}
.rbtn2:hover{border-color:var(--acc)}
.wbtn.on{border-color:color-mix(in srgb,var(--grn) 55%,var(--line));color:var(--grn);
background:color-mix(in srgb,var(--grn) 10%,var(--panel))}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{font-size:12.5px;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:4px 12px;color:var(--fg);cursor:pointer;transition:border-color .12s,color .12s}
.chip:hover{border-color:var(--acc);color:var(--acc);text-decoration:none}
article p{margin:.7em 0}article ul,article ol{padding-left:1.3em}article li{margin:.25em 0}
blockquote{border-left:3px solid var(--acc);margin:1em 0;padding:.2em 1em;color:var(--mut);background:var(--panel)}
code{background:var(--panel);border:1px solid var(--line);padding:1px 5px;border-radius:4px;font-size:.88em}
pre{background:var(--panel);border:1px solid var(--line);padding:12px 14px;border-radius:8px;overflow-x:auto}
hr{border:0;border-top:1px solid var(--line);margin:1.6em 0}
.tablewrap{overflow-x:auto;margin:.4em 0}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border-bottom:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:middle}
thead th{border-bottom:2px solid var(--line);font-weight:600;color:var(--mut);font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}
tbody tr{transition:background .1s}tbody tr:hover td{background:var(--panel)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.neg{color:var(--neg)}
.tklink{font-weight:600}.postable td{padding:11px 12px}.postable .price{font-weight:500}
.chg.up{color:var(--pos)}.chg.down{color:var(--neg)}.cat{color:var(--mut);font-size:13px}
.market{display:flex;align-items:center;gap:18px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:14px 18px;margin:.4em 0 1.1em}
.mbody{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;flex:1}
.mtk{font-size:20px;font-weight:600}.mprice{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.mchg{font-size:14px;font-weight:500}.mchg.up{color:var(--pos)}.mchg.down{color:var(--neg)}
.mmeta{font-size:13px;color:var(--mut)}.mload{font-size:13px;color:var(--fade)}
.spark{flex-shrink:0;line-height:0}.spark svg{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(150px,100%),1fr));gap:10px;margin:.4em 0 .6em}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tk{font-size:12px;color:var(--mut);margin-bottom:4px}.tv{font-size:21px;font-weight:600;font-variant-numeric:tabular-nums}
.tv.neg{color:var(--neg)}.tp{font-size:11px;color:var(--fade);margin-top:2px}
.hi{margin:.2em 0 .4em}.hi li{margin:.2em 0}
.flags{display:flex;flex-direction:column;gap:8px}
.flag{display:flex;gap:10px;align-items:flex-start;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px 13px;font-size:14px}
.flag .dot{width:9px;height:9px;border-radius:50%;margin-top:6px;flex-shrink:0}
.dot.red{background:var(--red)}.dot.yellow{background:var(--yel)}.dot.green{background:var(--grn)}
.feed{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--panel)}
.widget .feed{border:none;border-radius:0}
.fitem{border-bottom:1px solid var(--line)}.fitem:last-child{border-bottom:none}
.fhead{display:flex;align-items:baseline;gap:12px;padding:14px 18px;cursor:pointer;transition:background .1s;font-size:14.5px}
.fhead:hover{background:var(--bg)}
.fdate{font-size:12.5px;color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap;min-width:84px}
.fsrc{font-size:12.5px;color:var(--mut);white-space:nowrap;max-width:92px;overflow:hidden;text-overflow:ellipsis}
.ftk{font-size:12.5px;font-weight:600;color:var(--acc);white-space:nowrap;min-width:46px}
.fdesc{flex:1 1 12rem;min-width:0;overflow-wrap:anywhere}
.fbody{max-height:0;overflow:hidden;transition:max-height .22s ease;padding:0 18px}
.fitem.open .fbody{max-height:420px;padding:2px 18px 15px 114px}
.fsum{font-size:14px;color:var(--mut);line-height:1.55;margin-bottom:9px}
.fopen{display:inline-block;font-weight:500;font-size:13.5px}
.fnlink{display:block;padding:7px 0;font-size:14px;color:var(--fg);border-bottom:1px solid var(--line)}
.fnlink:last-child{border-bottom:none}.fnlink:hover{color:var(--acc);text-decoration:none}
.fnsrc{color:var(--mut);font-size:12px;margin-right:8px;font-weight:500}
.fcount{color:var(--fade);font-size:12px;font-weight:500}
.badge{font-size:11.5px;padding:2px 8px;border-radius:5px;background:var(--bg);border:1px solid var(--line);color:var(--mut);white-space:nowrap}
.badge.mat{border-color:var(--acc);color:var(--acc)}.badge.earn{background:var(--acc);border-color:var(--acc);color:#fff}
.lenswrap{margin:.3em 0 .4em}
.verdict{border:1px solid var(--line);border-left:4px solid var(--mut);border-radius:12px;padding:15px 18px;margin-bottom:14px;background:var(--panel)}
.verdict.buy{border-left-color:var(--grn)}.verdict.sell{border-left-color:var(--red)}.verdict.warn{border-left-color:var(--yel)}
.vlab{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--fade);margin-bottom:3px}
.vsig{font-size:21px;font-weight:600;display:flex;align-items:baseline;gap:10px}
.vconf{font-size:12px;font-weight:400;color:var(--mut);text-transform:none}
.vsum{font-size:14px;color:var(--mut);margin-top:6px;max-width:78ch}.vbar{font-size:12.5px;color:var(--muted,#8a8a8a);margin:2px 0 6px}.vbar b{font-weight:600;color:inherit}
.lensgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(232px,100%),1fr));gap:10px}
.lens{border:1px solid var(--line);border-radius:11px;padding:13px 15px;background:var(--panel);transition:border-color .12s}
.lens:hover{border-color:var(--mut)}
.lhead{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:3px}
.lname{font-size:13.5px;font-weight:500}
.pill{font-size:11px;font-weight:600;padding:2px 9px;border-radius:20px;white-space:nowrap}
.pill.buy{background:color-mix(in srgb,var(--grn) 11%,var(--panel));color:var(--grn);border:1px solid color-mix(in srgb,var(--grn) 55%,var(--line))}
.pill.sell{background:color-mix(in srgb,var(--red) 11%,var(--panel));color:var(--red);border:1px solid color-mix(in srgb,var(--red) 55%,var(--line))}
.pill.warn{background:color-mix(in srgb,var(--yel) 11%,var(--panel));color:var(--yel);border:1px solid color-mix(in srgb,var(--yel) 55%,var(--line))}
.pill.neu{background:var(--bg);color:var(--mut);border:1px solid var(--line)}
.lconf{font-size:11px;color:var(--fade);margin-bottom:5px}
.cbar{height:4px;border-radius:3px;background:var(--line);overflow:hidden;margin-bottom:8px}
.cbar span{display:block;height:100%;border-radius:3px;background:var(--mut)}
.cbar.buy span{background:var(--grn)}.cbar.sell span{background:var(--red)}.cbar.warn span{background:var(--yel)}
.lnote{font-size:13px;color:var(--mut);line-height:1.5}
.theadbtns{display:flex;gap:10px;flex-wrap:wrap}
.pchart{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin:.2em 0 1.4em;box-shadow:var(--shadow)}
.ranges{display:flex;gap:6px;margin-bottom:12px;min-width:0;flex-wrap:wrap}
.rbtn{border:1px solid var(--line);background:transparent;color:var(--mut);border-radius:8px;padding:6px 13px;font-size:13px;font-weight:500;cursor:pointer}
.rbtn:hover{border-color:var(--acc);color:var(--acc)}.rbtn.on{background:var(--accbg);border-color:var(--acc);color:var(--acc)}
.chartbox{position:relative;height:320px;width:100%}
.alloclegend{margin-top:14px;display:flex;flex-direction:column;gap:8px}
.alli{display:flex;align-items:center;gap:9px;font-size:14px}.alldot{width:11px;height:11px;border-radius:3px;flex-shrink:0}
.alll{flex:1;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.allv{font-weight:600;font-variant-numeric:tabular-nums}
.morerow td{padding:0!important;border:none!important}
.morebtn{width:100%;background:transparent;border:none;border-top:1px solid var(--line);color:var(--acc);font-size:13.5px;font-weight:500;padding:13px;cursor:pointer}
.morebtn:hover{background:var(--bg)}
.rstatus{background:var(--accbg);border:1px solid color-mix(in srgb,var(--acc) 45%,var(--line));border-radius:12px;padding:13px 18px;margin:.2em 0 1.3em;font-size:14.5px;display:flex;align-items:center;gap:10px}
.rstatus{flex-wrap:wrap}
.rbar{flex-basis:100%;height:6px;border-radius:4px;background:var(--line);overflow:hidden;margin:10px 0 0}
.rbarfill{height:100%;background:var(--acc);border-radius:4px;transition:width .25s ease}
.rsteps{flex-basis:100%;display:flex;gap:3px;margin-top:6px}
.rstep{flex:1;height:4px;border-radius:2px;background:var(--line);min-width:4px}
.rstep.on{background:var(--acc)}
.rmeta{flex-basis:100%;font-size:12.5px;color:var(--mut);margin-top:7px}
.rstatus.err .rbarfill{background:var(--warn,#b0851f)}.rstatus.err .rstep.on{background:var(--warn,#b0851f)}
.rstatus .btn{margin-top:10px}
.rstatus.done{background:color-mix(in srgb,var(--grn) 10%,var(--panel));border-color:color-mix(in srgb,var(--grn) 55%,var(--line))}
.rstatus.err{background:color-mix(in srgb,var(--red) 9%,var(--panel));border-color:color-mix(in srgb,var(--red) 50%,var(--line));color:var(--red);font-weight:500}
.rmsg{color:var(--mut);font-size:13px}
.spin{width:15px;height:15px;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;display:inline-block;animation:spin 1s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.refresh{color:var(--acc)!important;font-weight:500}
.navtop{font-weight:500;margin:2px 0 6px;padding-left:10px}
.thead{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.addbar{display:flex;gap:8px;margin:.2em 0 .4em;min-width:0;max-width:100%}
/* /research carries the Add form in its header: at 320px it pushed the page 22px sideways
   (form right edge at 342px). The header row, its actions and the form may all shrink. */
.pagehead>*,.headactions,.headactions>form{min-width:0}.headactions{flex-wrap:wrap}
.addbar input{flex:1 1 0;width:100%;min-width:0;max-width:280px;height:36px;padding:0 12px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--fg);font-size:14px;font-family:inherit}
.addbar input:focus{outline:none;border-color:var(--acc)}
.pfsum{display:flex;gap:22px;flex-wrap:wrap;margin:.2em 0 .2em}.pfsum:empty{display:none}
.pfstat .pfk{font-size:12px;color:var(--mut)}.pfstat .pfv{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.pfstat .pfv.up{color:var(--pos)}.pfstat .pfv.down{color:var(--neg)}
.pfempty{background:var(--panel);border:1px dashed var(--line);border-radius:12px;padding:20px 22px;color:var(--mut);font-size:14px;margin:.4em 0}
.board{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(220px,100%),1fr));gap:12px}
.pcard{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;cursor:pointer;transition:border-color .12s,transform .08s}
.pcard:hover{border-color:var(--acc)}.pcard:active{transform:scale(.995)}
.pctop{display:flex;align-items:center;justify-content:space-between}
.pctk{font-size:17px;font-weight:600}
.rm{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;flex-shrink:0;
border:1px solid var(--line);background:var(--panel);color:var(--mut);font-size:15px;line-height:1;
cursor:pointer;border-radius:50%;transition:color .1s,border-color .1s}
.rm:hover{color:var(--red);border-color:var(--red);background:color-mix(in srgb,var(--red) 8%,var(--panel))}
.pcname{font-size:13px;color:var(--mut);margin:1px 0 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pcquote{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;min-height:30px}
.pcprice{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.pcchg{font-size:13px;font-weight:500}.pcchg.up{color:var(--pos)}.pcchg.down{color:var(--neg)}
.pcquote .spark{margin-left:auto}
.pcfoot{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:11px}
.pccat{font-size:12px;color:var(--fade)}
.addbtn{display:inline-block;margin-left:6px;border:1px solid var(--line);border-radius:5px;padding:0 6px;font-size:11px;color:var(--mut);cursor:pointer;text-decoration:none}
.addbtn:hover{border-color:var(--acc);color:var(--acc);text-decoration:none}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);
padding:9px 18px;border-radius:22px;font-size:13.5px;font-weight:500;z-index:50;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:.95}
.mobilebar{display:none}.scrim{display:none}
@media(max-width:820px){
.mobilebar{display:flex;align-items:center;gap:8px;position:sticky;top:0;z-index:25;background:var(--panel);
border-bottom:1px solid var(--line);padding:9px 12px}
.burger{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:8px;color:var(--fg);cursor:pointer}
.burger:active{background:var(--bg)}
body{flex-direction:column}
.side{position:fixed;left:0;top:0;bottom:0;width:290px;height:100dvh;z-index:40;transform:translateX(-102%);transition:transform .22s ease}
#nav:checked~.side{transform:none;box-shadow:0 0 44px rgba(0,0,0,.3)}
.scrim{display:block;position:fixed;inset:0;background:rgba(10,10,10,.42);z-index:35;opacity:0;pointer-events:none;transition:opacity .2s}
#nav:checked~.scrim{opacity:1;pointer-events:auto}
main{padding:16px 16px 60px;max-width:100%}
.pagehead h1{font-size:28px}
.stat.wide{grid-column:span 1}.fitem.open .fbody{padding-left:18px}
.fhead{flex-wrap:wrap;row-gap:4px}
.kpirow{grid-template-columns:repeat(2,1fr);gap:10px}
.kpi{padding:14px 16px;border-radius:14px}.kk{font-size:11.5px;margin-bottom:6px}
.kv{font-size:21px}.kpi.hero{grid-column:span 2}.kpi.hero .kv{font-size:32px}
.dt{font-size:13.5px}.dt th{padding:10px 13px}.dt td{padding:12px 13px}
.dt .tklink{font-size:14.5px}.dt .cat{font-size:12.5px}
.whead{padding:14px 16px}.wtitle{font-size:15px}.wbody.pad{padding:14px 14px}
.widget{border-radius:14px}.chips{gap:5px}
.headactions{width:100%}.headactions .btn,.headactions .connectbtn{flex:1;justify-content:center}}
/* phone leftovers from the 2026-09-22 audit: 44px taps, 16px inputs (iOS zooms on anything
   smaller and stays zoomed), a drawer whose scroll never carries through to the page, and KPI
   tiles that shrink instead of pushing / 1px wider than a 320px screen */
/* (body-prefixed: the base rules for these sit further down the sheet and would win a tie) */
@media(max-width:820px){
body .btn,body .wbtn,body .rbtn2,body .connectbtn,body .rbtn,body .rfr{min-height:44px}body .rbtn{padding:0 12px}
body .fbform textarea,body .fbform input,body .jtk,body .jform select,body .jform .jnum,body .jform textarea,
body .jform input,body .jform #jd-source{font-size:16px}
body .jtk,body .jform select,body .jform .jnum,body .jform #jd-source{height:44px}
.side{overscroll-behavior:contain}
.kpirow{grid-template-columns:repeat(2,minmax(0,1fr))}.kpirow>*{min-width:0}.kv{overflow-wrap:anywhere}}
/* ---- key stats grid ---- */
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(178px,100%),1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:0 0 22px}
.stat{background:var(--panel);padding:13px 16px;min-width:0}
.stat.wide{grid-column:span 2}
.stk{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;margin-bottom:5px}
.stv{font-size:15.5px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.35}
.stv.up{color:var(--pos)}.stv.down{color:var(--neg)}
#st-52{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:500}
.rbar{flex:1;height:4px;border-radius:3px;background:var(--line);position:relative;min-width:56px}
.rmark{position:absolute;top:50%;left:0;width:11px;height:11px;border-radius:50%;background:var(--acc);border:2px solid var(--panel);transform:translate(-50%,-50%)}
/* ---- tabs ---- */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:4px 0 20px;overflow-x:auto}
.tab{border:none;background:none;color:var(--mut);font-family:inherit;font-size:14px;font-weight:500;padding:9px 14px;
cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap}
.tab:hover{color:var(--fg)}.tab.on{color:var(--fg);border-bottom-color:var(--acc);font-weight:600}
.tcount{font-size:11px;color:var(--fade);background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:1px 7px;margin-left:3px}
/* no animation on pane.on: switching a tab is the one thing that must feel like a
   light switch. The 180ms fade here WAS the "slightest delay" on the button tabs. */
.pane{display:none}.pane.on{display:block}
/* ---- research library ---- */
.doclist{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--panel);margin-bottom:6px}
.docrow{display:flex;align-items:center;gap:11px;padding:12px 16px;border-bottom:1px solid var(--line);color:var(--fg);font-size:14px;min-width:0}
.docrow:last-child{border-bottom:none}.docrow:hover{background:var(--bg);text-decoration:none}
.docrow .ic{color:var(--fade)}
.dlabel{font-weight:500;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dfile{font-size:12px;color:var(--fade);font-family:ui-monospace,monospace;flex-shrink:0}
/* ---- doc reader ---- */
/* reports: one fluid rule, no breakpoints — the article column is a TYPE measure
   (ch of the same fluid font-size the article uses, set here because ch resolves
   against THIS element's font), so the text column grows continuously with the
   screen; the toc rail clamps; the pair centers so leftover space balances */
.docgrid{display:grid;grid-template-columns:minmax(0,112ch) clamp(12rem,14vw,15rem);
  gap:clamp(28px,3vw,64px);align-items:start;justify-content:center;
  font-size:clamp(15px,.32vw + 11.5px,19px)}
.doctoc{position:sticky;top:32px;font-size:13px;max-height:calc(100vh - 64px);overflow:auto;border-left:1px solid var(--line);padding-left:16px}
.tochead{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--fade);font-weight:600;margin-bottom:8px}
.tocl{display:block;color:var(--mut);padding:3.5px 0;line-height:1.4}
.tocl:hover{color:var(--acc);text-decoration:none}
.tocl.sub{padding-left:14px;font-size:12.5px}.tocl.on{color:var(--acc);font-weight:500}
@media(max-width:1150px){.docgrid{display:block}.doctoc{display:none}}
.csep{color:var(--fade);margin:0 6px}
.crumb a{color:var(--fade)}.crumb a:hover{color:var(--acc);text-decoration:none}
/* ---- digest filters / skeletons / misc ---- */
.ffilters{display:flex;gap:4px}
.fbtn{border:1px solid var(--line);background:transparent;color:var(--mut);border-radius:7px;padding:4px 10px;
font-size:12px;font-weight:500;font-family:inherit;cursor:pointer}
.fbtn:hover{border-color:var(--acc);color:var(--acc)}.fbtn.on{background:var(--accbg);border-color:var(--acc);color:var(--acc)}
.skelrow{display:flex;gap:10px;padding:7px 0}
.skel{height:12px;border-radius:6px;background:linear-gradient(90deg,var(--line),var(--bg),var(--line));background-size:200% 100%;animation:shimmer 1.2s infinite}
.skel.w1{width:70px}.skel.w2{width:38%}.skel.w3{width:22%}
@keyframes shimmer{from{background-position:200% 0}to{background-position:-200% 0}}
.thead .tmeta{font-size:14px;margin-top:-2px}
.txpill{font-size:11.5px;font-weight:600;padding:3px 10px;border-radius:6px;white-space:nowrap;
background:var(--bg);border:1px solid var(--line);color:var(--mut)}
.txpill.buy{background:color-mix(in srgb,var(--grn) 11%,var(--panel));color:var(--grn);border-color:color-mix(in srgb,var(--grn) 50%,var(--line))}
.txpill.sell{background:color-mix(in srgb,var(--red) 11%,var(--panel));color:var(--red);border-color:color-mix(in srgb,var(--red) 50%,var(--line))}
.txpill.div{background:var(--accbg);color:var(--acc);border-color:color-mix(in srgb,var(--acc) 45%,var(--line))}
.txname{font-size:13.5px;color:var(--mut)}
.txdate{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--mut);font-size:13.5px}
.amt.up{color:var(--pos)}
.recdate{display:flex;align-items:center;gap:10px;margin:-8px 0 14px;font-size:13.5px}
.dt th.sortable{cursor:pointer;user-select:none}.dt th.sortable:hover{color:var(--fg)}.dt th .sarr{margin-left:4px}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.ngrp{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--fade);font-weight:600;padding:10px 10px 3px}
.addbtn.done{border:none;color:var(--pos);cursor:default;font-size:12px;padding:0 4px}
.pfsum2{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;margin:0 0 12px}
.pfsum2:empty{display:none}
.pfsum2 .pfs{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.pfsum2 .pfs.up{color:var(--pos)}.pfsum2 .pfs.down{color:var(--neg)}
.pfsum2 .pfsl{font-size:12.5px;color:var(--mut);font-variant-numeric:tabular-nums}
.pfsum2 .pfsl i{font-style:normal;color:var(--fade)}
.pfsum2 .pfsl.warnl{color:var(--warn,#b0851f);font-weight:500}
.fbform{display:flex;gap:10px;align-items:flex-end}
.fbform textarea{flex:1;resize:vertical;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--fg);font:inherit;font-size:14px}
.fbform textarea:focus{outline:none;border-color:var(--acc)}
.fblist{display:flex;flex-direction:column;margin-bottom:12px}
.fbitem{display:flex;align-items:baseline;gap:12px;padding:9px 2px;border-bottom:1px solid var(--line);font-size:14px}
.fbitem:last-child{border-bottom:none}
.fbdate{font-size:12px;color:var(--fade);font-variant-numeric:tabular-nums;white-space:nowrap}
.fbmsg{flex:1;line-height:1.5}
.lotst{font-size:13.5px}.lotst td,.lotst th{padding:8px 12px}.ltcell{white-space:nowrap;font-variant-numeric:tabular-nums;font-size:13px}
.planwrap{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:6px 16px;margin-bottom:6px}
.planrow{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid var(--line)}
.planrow:last-of-type{border-bottom:none}
.plk{font-size:13px;font-weight:600;min-width:180px}
.plv{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.pld{font-size:13px;color:var(--mut);font-variant-numeric:tabular-nums;margin-left:auto}
.pld.near{color:var(--yel);font-weight:600}
.plnote{font-size:13px;color:var(--mut);line-height:1.5;padding:9px 0;border-top:1px solid var(--line)}
@media(max-width:640px){.lotst th:nth-child(5),.lotst td:nth-child(5){display:none}.plk{min-width:0}}
.jtk{width:74px;height:36px;padding:0 10px;border:1px solid var(--line);border-radius:9px;background:var(--bg);
color:var(--fg);font:600 13px/1 Inter,sans-serif;text-transform:uppercase}
.jtk:focus{outline:none;border-color:var(--acc)}
.jform{display:flex;flex-direction:column;gap:9px;margin-bottom:6px}
.jformrow{display:flex;gap:9px;flex-wrap:wrap}
.jform select,.jform .jnum{height:36px;padding:0 10px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--fg);font:14px Inter,sans-serif}
.jform .jnum{width:90px}
.jform textarea,.jform input{font-family:inherit}
.jform textarea{resize:vertical;padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--fg);font-size:14px}
.jform textarea:focus,.jform select:focus,.jform .jnum:focus,.jform input:focus{outline:none;border-color:var(--acc)}
.jform #jd-source{height:36px;padding:0 10px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--fg);font-size:13.5px}
.jform button{align-self:flex-start}
.jcard{border:1px solid var(--line);border-radius:12px;padding:13px 16px;margin:8px 0;background:var(--panel)}
.jhead{display:flex;align-items:center;gap:11px;flex-wrap:wrap;margin-bottom:7px;font-size:13.5px}
.jrow{font-size:14px;line-height:1.55;margin:3px 0;display:flex;gap:10px}
.jrow b{flex-shrink:0;min-width:64px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;padding-top:3px}
/* ---- thesis card ---- */
.tcard{border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:14px;background:var(--panel);
padding:16px 20px;margin:0 0 18px;box-shadow:var(--shadow)}
.tchead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;font-size:13px}
.tcthesis{font-size:15.5px;font-weight:600;line-height:1.5;letter-spacing:-.005em}
.tcnote{font-size:13.5px;color:var(--mut);margin-top:5px;line-height:1.55}
.tccols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr));gap:4px 22px;margin-top:12px}
.tccol ul{margin:4px 0 0;padding-left:1.15em}
.tccol li{font-size:13.5px;line-height:1.5;margin:.25em 0}
.tclab{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:600;margin-top:6px}
.tclab.kill{color:var(--red)}
.tcladder{display:flex;flex-direction:column;margin-top:4px}
.tcrung{display:flex;align-items:baseline;gap:12px;padding:6px 0;border-bottom:1px solid var(--line);font-size:13.5px;flex-wrap:wrap}
.tcrung:last-child{border-bottom:none}
.tcz{font-weight:600;font-variant-numeric:tabular-nums;min-width:86px}
.tca{color:var(--fg)}
.tcs{margin-left:auto;font-size:12px;color:var(--fade)}
.tcs.live{color:var(--grn);font-weight:600}
.tcmsrow{display:flex;gap:8px 18px;flex-wrap:wrap;margin-top:12px;padding-top:11px;border-top:1px solid var(--line)}
.tcms{font-size:12.5px;color:var(--mut)}.tcms b{font-variant-numeric:tabular-nums;color:var(--fg);font-weight:600}
.fold{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin:12px 0;padding:0 16px}
.fold>summary{padding:13px 0;font-size:14px;font-weight:600;text-transform:none;letter-spacing:0;color:var(--fg)}
.fold>summary .hint{font-weight:400}
.fold[open]{padding-bottom:14px}
.stale{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 14px;font-size:13.5px}
.quietlog{border-top:1px solid var(--line)}
.quietlog>summary{padding:12px 24px;font-size:12px;color:var(--mut)}
.quietlog[open]>summary{border-bottom:1px solid var(--line)}
.archrow{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
.btn.subtle{color:var(--mut)}.btn.subtle:hover{color:var(--red);border-color:var(--red)}
.hint2{font-size:12px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = r"""
function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function fmtCap(n){if(!n)return'';return n>=1e9?('$'+(n/1e9).toFixed(2)+'B'):('$'+(n/1e6).toFixed(0)+'M');}
function watchCell(s){
 var link="<a class=tklink data-tk='"+s+"' href='/ticker/"+s+"'>"+s+"</a>";
 if(HELD.indexOf(s)>=0)return link+'<span class="addbtn done" title="held">●</span>';
 if(WATCH.indexOf(s)>=0)return link+'<span class="addbtn done" title="watching">✓</span>';
 return link+"<a class=addbtn data-wtk='"+s+"' title='add to watchlist' onclick='boardAdd(event,\""+s+"\")'>+ watch</a>";}
function fmtTables(){
 var numRe=/^\s*[-+(−~]?\$?\d[\d,]*\.?\d*[%BMKx]?\)?\s*$/;
 document.querySelectorAll('#main table').forEach(function(tbl){
  var head=tbl.tHead&&tbl.tHead.rows[0];
  if(head){ /* any Symbol/Ticker column in any table gets link + one-tap watch */
   for(var ci=0;ci<head.cells.length;ci++){
    if(!/^(symbol|ticker)s?$/i.test((head.cells[ci].textContent||'').trim()))continue;
    var rs=tbl.tBodies[0]?tbl.tBodies[0].rows:[];
    for(var i=0;i<rs.length;i++){var c=rs[i].cells[ci];if(!c||c.children.length)continue;var s=c.textContent.trim();
     if(/^[A-Z][A-Z.\-]{0,6}$/.test(s))c.innerHTML=watchCell(s);}}}
  if(tbl.classList.contains('postable'))return;
  var rows=tbl.tBodies[0]?tbl.tBodies[0].rows:[];if(!rows.length)return;
  for(var c=0;c<rows[0].cells.length;c++){var num=0,tot=0;
   for(var r=0;r<rows.length;r++){var x=rows[r].cells[c];if(!x)continue;tot++;
    if(x.classList.contains('num')||numRe.test(x.textContent))num++;}
   if(tot&&num/tot>=0.6){for(var r=0;r<rows.length;r++){var x=rows[r].cells[c];if(!x)continue;
    x.classList.add('num');var t=x.textContent.trim();if(t[0]==='-'||t[0]==='('||t[0]==='−')x.classList.add('neg');}
    if(tbl.tHead&&tbl.tHead.rows[0]&&tbl.tHead.rows[0].cells[c])tbl.tHead.rows[0].cells[c].classList.add('num');}}
 });
}
function renderMarketHead(el,d){
 if(!d||!d.price){var l=el.querySelector('.mload');if(l)l.textContent='live quote unavailable';return;}
 var chg=d.prev?(d.price-d.prev):0,pct=d.prev?(chg/d.prev*100):0,up=chg>=0,meta=[];
 var h='<span class=mtk>'+el.dataset.ticker+'</span><span class=mprice>$'+d.price.toFixed(2)+'</span>';
 h+='<span class="mchg '+(up?'up':'down')+'">'+(up?'▲':'▼')+' '+Math.abs(chg).toFixed(2)+' ('+Math.abs(pct).toFixed(1)+'%)</span>';
 if(d.cap)meta.push('Mkt cap '+fmtCap(d.cap));if(d.ylo&&d.yhi)meta.push('52wk $'+d.ylo.toFixed(2)+'–$'+d.yhi.toFixed(2));
 meta.push('as of '+new Date(d.at?d.at*1000:Date.now()).toLocaleTimeString());   /* the price's own time (d.at, the server's fetch) */
 el.innerHTML=h+'<span class=mmeta>'+meta.join(' · ')+'</span>';}
function drawSpark(el,vals){
 if(!vals||vals.length<2)return;var w=104,h=30,mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals),rng=(mx-mn)||1;
 var pts=vals.map(function(v,i){return (i/(vals.length-1)*w).toFixed(1)+','+(h-2-(v-mn)/rng*(h-4)).toFixed(1)}).join(' ');
 var up=vals[vals.length-1]>=vals[0],col=up?getComputedStyle(document.documentElement).getPropertyValue('--pos'):getComputedStyle(document.documentElement).getPropertyValue('--neg');
 el.innerHTML='<svg viewBox="0 0 '+w+' '+h+'" width="'+w+'" height="'+h+'" preserveAspectRatio="none"><polyline points="'+pts+'" fill="none" stroke="'+col.trim()+'" stroke-width="1.6" stroke-linejoin="round"/></svg>';
}
function loadSparks(){document.querySelectorAll('[data-spark]').forEach(function(el){
 fetch('/api/spark?ticker='+encodeURIComponent(el.dataset.spark)).then(function(r){return r.json()}).then(function(v){drawSpark(el,v)}).catch(function(){});});}
function setk(id,txt,cls){var e=document.getElementById(id);if(e){e.textContent=txt;e.className='kv'+(cls?(' '+cls):'');}}
function money(n){return '$'+Math.round(n).toLocaleString();}
function signed(n){return (n>=0?'+':'−')+money(Math.abs(n));}
/* ONE request for every quote the page needs — holdings rows, sidebar rows, the market
   header. It used to be one fetch per ticker, and computeAccount() could not run until
   the last of them returned, so on the phone the Account widget waited out the slowest
   round trip of four (David 2026-08-13: "why is tab switching in brokerb so much
   faster than portfolio"). */
function batchQuotes(tks,cb){
 tks=tks.filter(function(t,i){return t&&tks.indexOf(t)===i});
 if(!tks.length){cb({});return;}
 fetch('/api/quotes?tickers='+encodeURIComponent(tks.join(','))).then(function(r){return r.json()})
  .then(function(d){cb(d||{})}).catch(function(){cb({})});}
/* Sidebar Holdings (2026-09-22): today's $ on OUR position and the % move, per row and per book.
   The server renders each row with its last known value; the live numbers ride the page's one
   quote batch below — on a page render for rows that are new, and on the live tick (liveTick,
   every MKT.owned_s in market hours) for all of them. */
var _holdAt=0;
function holdDay(sh,d){ /* shares × (price − prev close): the same arithmetic as app.day_change() */
 sh=parseFloat(sh)||0;if(!d||!d.price||!d.prev||!sh)return null;
 return {usd:sh*(d.price-d.prev),pct:(d.price-d.prev)/d.prev*100,value:sh*d.price};}
function fillHoldings(hold,quotes){
 hold.forEach(function(el){el.dataset.done='1';var x=holdDay(el.dataset.sh,quotes[el.dataset.q]);
  if(!x)return;   /* no quote: the server-rendered last known value stays — never a blank row */
  var up=x.usd>=0,c=up?' up':' down',v=el.querySelector('.hv'),hd=el.querySelector('.hd'),hp=el.querySelector('.hp');
  el.dataset.usd=x.usd.toFixed(2);
  if(v)v.textContent=money(x.value);
  if(hd){hd.textContent=signed(x.usd);hd.className='hd'+c;}
  if(hp){var ap=Math.abs(x.pct);hp.textContent=(up?'+':'−')+ap.toFixed(ap>0&&ap<0.1?2:1)+'%';hp.className='hp'+c;}});
 document.querySelectorAll('[data-hday]').forEach(function(h){var t=0,n=0;
  document.querySelectorAll('.hrow[data-book="'+h.dataset.hday+'"][data-usd]').forEach(function(r){t+=parseFloat(r.dataset.usd)||0;n++;});
  if(n){h.textContent=signed(t);h.className='hday '+(t>=0?'up':'down');}});}
/* tier: none = a page render (every quote on the page; Holdings rows that are new, or all of them
   once older than the owned beat while the market is open); 'owned' = the live tick for what we
   own — Holdings, the Book's held rows, an owned name's header and stats — and nothing else;
   'all' = the slower live tick, every quote on screen. */
function fillPositions(tier){
 var own=tier==='owned'?function(tk){return HELD.indexOf(tk)>=0;}:null;
 var ac=document.getElementById('acct');
 var cash=ac?parseFloat(ac.dataset.cash)||0:0,mmf=ac?parseFloat(ac.dataset.mmf)||0:0;
 var rows=Array.prototype.slice.call(document.querySelectorAll('.posrow[data-tk]'));
 if(own)rows=rows.filter(function(r){return parseFloat(r.dataset.shares)>0;});
 var mkt=Array.prototype.slice.call(document.querySelectorAll('.mbody[data-ticker]'));
 var stg=tier?Array.prototype.slice.call(document.querySelectorAll('.statgrid[data-stats]')):[];   /* a render has loadStats() */
 if(own){mkt=mkt.filter(function(e){return own(e.dataset.ticker)});stg=stg.filter(function(e){return own(e.dataset.stats)});}
 var all=tier||(MKT.open&&Date.now()-_holdAt>MKT.owned_s*1000);
 var hold=Array.prototype.slice.call(document.querySelectorAll(all?'.hrow[data-q]':'.hrow[data-q]:not([data-done])'));
 /* a book with no public rows yet (DIP Venture, all cash) still has an account to total and an allocation to draw */
 if(ac&&!rows.length&&!tier)computeAccount([],{},cash,mmf);
 if(!rows.length&&!mkt.length&&!hold.length&&!stg.length)return;
 if(hold.length)_holdAt=Date.now();
 var tks=rows.map(function(r){return r.dataset.tk})
   .concat(mkt.map(function(e){return e.dataset.ticker}))
   .concat(stg.map(function(e){return e.dataset.stats}))
   .concat(hold.map(function(e){return e.dataset.q}));
 batchQuotes(tks,function(quotes){
  if(hold.length)fillHoldings(hold,quotes);
  rows.forEach(function(row){
   var d=quotes[row.dataset.tk],p=row.querySelector('.price'),c=row.querySelector('.chg');
   if(d&&d.price){if(p)p.textContent='$'+d.price.toFixed(2);
    if(c){var chg=d.prev?(d.price-d.prev):0,pct=d.prev?chg/d.prev*100:0,up=chg>=0;c.textContent=(up?'+':'')+pct.toFixed(1)+'%';c.className='num chg '+(up?'up':'down');}
   }else if(p)p.textContent='—';
  });
  mkt.forEach(function(el){renderMarketHead(el,quotes[el.dataset.ticker])});
  stg.forEach(function(g){renderStats(g,quotes[g.dataset.stats])});
  if(rows.length)computeAccount(rows,quotes,cash,mmf,tier==='owned');
 });
}
/* quiet: the owned tick re-totals the Book without redrawing the allocation doughnut (and
   re-fetching /api/external) every few seconds — the slower tick and a page render draw it */
function computeAccount(rows,quotes,cash,mmf,quiet){
 var ac=document.getElementById('acct'),note=ac?parseFloat(ac.dataset.note)||0:0,priv=ac?parseFloat(ac.dataset.private)||0:0;
 var stocks=0,dayp=0,pnl=0,cost=0;
 rows.forEach(function(row){var sh=parseFloat(row.dataset.shares)||0,d=quotes[row.dataset.tk];
  if(sh>0&&d&&d.price){var mv=sh*d.price,cb=sh*(parseFloat(row.dataset.cost)||0);
   stocks+=mv;cost+=cb;pnl+=mv-cb;dayp+=sh*(d.prev?(d.price-d.prev):0);}});
 var acctVal=stocks+cash+mmf+note+priv;
 rows.forEach(function(row){var sh=parseFloat(row.dataset.shares)||0,d=quotes[row.dataset.tk];
  var mv=row.querySelector('.mval'),pl=row.querySelector('.pnl'),wt=row.querySelector('.wt');
  if(sh>0&&d&&d.price){var v=sh*d.price,cb=sh*(parseFloat(row.dataset.cost)||0),g=v-cb,gp=cb?g/cb*100:0,up=g>=0;
   if(mv)mv.textContent=money(v);
   if(pl){pl.innerHTML=signed(g)+' <span class=pct>('+(up?'+':'−')+Math.abs(gp).toFixed(1)+'%)</span>';pl.className='num pnl '+(up?'up':'down');}
   if(wt)wt.textContent=(acctVal?v/acctVal*100:0).toFixed(1)+'%';}
  else{if(mv)mv.textContent='';if(pl)pl.textContent='';if(wt)wt.textContent='';}});
 setk('kv-val',money(acctVal));
 var dpct=(stocks-dayp)?dayp/(stocks-dayp)*100:0;
 setk('kv-day',signed(dayp)+' ('+(dayp>=0?'+':'−')+Math.abs(dpct).toFixed(1)+'%)',dayp>=0?'up':'down');
 var rpct=cost?pnl/cost*100:0;
 setk('kv-ret',signed(pnl)+' ('+(pnl>=0?'+':'−')+Math.abs(rpct).toFixed(1)+'%)',pnl>=0?'up':'down');
 setk('kv-cash',money(cash));
 var s=document.getElementById('ac-stocks');if(s)s.textContent=money(stocks);
 var t=document.getElementById('ac-total');if(t)t.textContent=money(acctVal);
 _acctVal=acctVal;_privVal=priv;updateLifetime();
 if(!quiet)renderAlloc(stocks,cash,mmf,note);
}
var _netdep=null,_lifesince='',_acctVal=null,_privVal=0;
function updateLifetime(){
 if(_netdep==null||_acctVal==null)return;
 /* private stakes (DIP: Waffle) sit in book value at a hand mark but are NOT P&L (David 2026-09-11):
    they were bought outside the brokerage feed, so neither the deposit nor the mark belongs here */
 var pl=(_acctVal-_privVal)-_netdep,pct=_netdep?pl/_netdep*100:0;
 setk('kv-life',signed(pl)+' ('+(pl>=0?'+':'−')+Math.abs(pct).toFixed(1)+'%)',pl>=0?'up':'down');
 var s=document.getElementById('kv-lifesub');
 if(s)s.textContent='vs '+money(_netdep)+' net deposited since '+(_lifesince||'').slice(0,7);}
function loadLifetime(){if(!document.getElementById('kv-life'))return;
 fetch('/api/lifetime').then(function(r){return r.json()}).then(function(d){
  if(d&&d.net_deposits!=null){_netdep=d.net_deposits;_lifesince=d.since||'';updateLifetime();}
 }).catch(function(){});}
var ALLOC_COLORS=['#2a78d6','#1baf7a','#eda100','#8e6fd6','#e87ba4','#eb6834','#6b6a61'];
function renderAlloc(stocks,cash,mmf,note){var cv=document.getElementById('allocchart');if(!cv||typeof Chart==='undefined')return;
 fetch('/api/external').then(function(r){return r.json()}).then(function(ext){
  var segs=[{l:'Stocks',v:stocks},{l:'Cash',v:cash},{l:'Money market',v:mmf},{l:'Structured note (SPX 2/28)',v:note||0}];
  (ext||[]).forEach(function(e){segs.push({l:e.name,v:e.value});});
  segs=segs.filter(function(s){return s.v>0});var tot=segs.reduce(function(a,s){return a+s.v},0)||1;
  if(_charts.alloc)_charts.alloc.destroy();
  _charts.alloc=new Chart(cv,{type:'doughnut',data:{labels:segs.map(function(s){return s.l}),
   datasets:[{data:segs.map(function(s){return s.v}),backgroundColor:ALLOC_COLORS,borderColor:cssvar('--panel'),borderWidth:2}]},
   options:{responsive:true,maintainAspectRatio:false,cutout:'62%',plugins:{legend:{display:false},
    tooltip:{callbacks:{label:function(c){return c.label+': '+money(c.raw)+' ('+(c.raw/tot*100).toFixed(0)+'%)'}}}}}});
  var lg=document.getElementById('alloclegend');
  if(lg)lg.innerHTML=segs.map(function(s,i){return '<div class=alli><span class=alldot style="background:'+ALLOC_COLORS[i%ALLOC_COLORS.length]+'"></span><span class=alll>'+esc(s.l)+'</span><span class=allv>'+(s.v/tot*100).toFixed(0)+'%</span></div>';}).join('');
 }).catch(function(){});
}
/* ---- book context (2026-09-11): the page says which book is on screen (#bookctx, set by
   home_inner); while it is not the BROKERA book every /api/ fetch carries book=<slug>, so the
   loaders below serve any book unchanged. Re-read on every nav, before enhance() fires. */
var BOOK='brokera',BOOK_HOME='/';
function readBook(){var b=document.getElementById('bookctx');BOOK=b?(b.dataset.book||'brokera'):'brokera';BOOK_HOME=b?(b.dataset.home||'/'):'/';}
(function(){var _f=window.fetch;window.fetch=function(u,o){
 if(typeof u==='string'&&u.indexOf('/api/')===0&&BOOK!=='brokera'&&u.indexOf('book=')<0)u+=(u.indexOf('?')>=0?'&':'?')+'book='+encodeURIComponent(BOOK);
 return _f.call(window,u,o);};})();
function syncBrokerage(){toast('Syncing '+(BOOK==='brokera'?'the Personal book':BOOK.toUpperCase())+'…');post('/api/aggregatora/sync?force=1').then(function(r){return r.json()}).then(function(d){
 if(d.ok){var got=(d.books&&d.books[BOOK])||d.synced;toast(d.warn?('⚠ '+d.warn):((got&&got.length)?('Synced '+got.join(', ')):(d.msg||'Synced')));nav(BOOK_HOME,false);refreshSide();}else{toast(d.msg||'sync failed');}}).catch(function(){toast('sync error');});}
/* auto-sync: when the dashboard is opened, sync once per tab in the background and refresh the
   numbers when done — in market hours if the brokerage snapshot is older than MKT.sync_s; after
   the close only if it predates the close (the live-data rule; st_sync() holds the same line) */
function autoSync(){
 if(typeof SYNCED==='undefined')return;
 if(MKT.open?Date.now()/1000-(SYNCED||0)<MKT.sync_s:(SYNCED||0)>=MKT.close)return;
 if(sessionStorage.getItem('autosynced'))return;
 sessionStorage.setItem('autosynced','1');
 var n=document.getElementById('syncnote');if(n)n.textContent='· syncing now…';
 post('/api/aggregatora/sync').then(function(r){return r.json()}).then(function(d){
  if(d.ok&&!d.skipped){SYNCED=Date.now()/1000;toast('Brokerage synced');refreshSide();
   if(location.pathname==='/')nav('/',false);}
  else if(n)n.textContent='';
 }).catch(function(){if(n)n.textContent='';});}
function post(u){return fetch(u,{method:'POST',headers:{'X-Requested-With':'fetch'}})}
/* JSON-body POST (same-origin by construction, like post()): the asks-answer route and any
   future mutation that carries a payload. fetch() with a JSON body needs the content-type
   set explicitly or Flask's get_json() sees nothing. */
function postJson(u,body){return fetch(u,{method:'POST',headers:{'X-Requested-With':'fetch','Content-Type':'application/json'},body:JSON.stringify(body||{})})}
function toast(msg){var t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.appendChild(t);
 requestAnimationFrame(function(){t.classList.add('show')});setTimeout(function(){t.classList.remove('show');setTimeout(function(){t.remove()},260)},1500);}
/* The sidebar is rendered OUTSIDE #main and nav() only swaps #main, so it was built
   once per full page load and never again. Every mutation therefore needed its own
   hand-written DOM surgery — remove and archive had some, add had none, and anything
   nobody wrote surgery for went silently stale until F5. One refresh path instead. */
/* resolves true once the sidebar is re-rendered, false if it was not (the live tick reads the
   market state off it only on true) */
function refreshSide(){
 var side=document.querySelector('.side');if(!side)return Promise.resolve(false);
 return fetch('/api/sidebar').then(function(r){if(!r.ok)throw r.status;return r.text()}).then(function(h){
  side.innerHTML=h;
  initSearch();                                   /* the search box was inside it */
  setActive(decodeURIComponent(location.pathname)+location.search);
  var hs=document.querySelector('[data-held]');   /* what we own now: a name bought since load joins the owned tick */
  if(hs){try{HELD=JSON.parse(hs.dataset.held);}catch(_){}}
  fillPositions();                                /* the new Holdings rows join one quote batch */
  return true;
 }).catch(function(){return false;});}
/* The watchlist and the research tree left the sidebar for /research (2026-09-22), so a watch or
   archive no longer re-renders the sidebar — the page that asked re-renders itself instead. */
function addWatch(tk,cb){postJson('/api/watchlist',{action:'add',ticker:tk}).then(function(r){return r.json()}).then(function(d){
 if(WATCH.indexOf(tk)<0)WATCH.push(tk);
 document.querySelectorAll('.addbtn[data-wtk="'+tk+'"]').forEach(function(b){b.outerHTML='<span class="addbtn done">✓</span>';});
 toast(tk+' added to watchlist');if(cb)cb(d);});}
function boardAdd(e,tk){e.stopPropagation();e.preventDefault();addWatch(tk);}
function removeWatch(e,tk,cb){if(e){e.stopPropagation();e.preventDefault();}
 postJson('/api/watchlist',{action:'remove',ticker:tk}).then(function(){
  var i=WATCH.indexOf(tk);if(i>=0)WATCH.splice(i,1);
  if(e&&e.target){var tr=e.target.closest('tr');if(tr)tr.remove();}
  document.querySelectorAll('.pcard[data-tk="'+tk+'"]').forEach(function(x){x.remove();});
  toast(tk+' removed from watchlist');if(cb)cb();});}
function here(){return location.pathname+location.search;}
function rsWatch(e,tk){e.stopPropagation();e.preventDefault();addWatch(tk,function(){nav(here(),false);});}
function rsUnwatch(e,tk){e.stopPropagation();e.preventDefault();removeWatch(null,tk,function(){nav(here(),false);});}
function archiveResearch(e,tk){e.stopPropagation();e.preventDefault();
 if(!confirm('Archive the '+tk+' research folder? It moves to _archive/ (reversible by moving it back).'))return;
 post('/api/research/archive?ticker='+encodeURIComponent(tk)).then(function(r){return r.json()}).then(function(d){
  toast(d.msg||'done');if(d.ok)nav(location.pathname.indexOf('/research')===0?here():'/ticker/'+tk,false);
 }).catch(function(){toast('error');});}
function fbSend(e){e.preventDefault();var t=document.getElementById('fbmsg'),v=t.value.trim();if(!v)return false;
 fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:v})})
  .then(function(){toast('Saved — every future digest will honor it');nav('/recommendation',false);}).catch(function(){toast('error');});
 return false;}
function fbDel(id){postJson('/api/feedback',{action:'remove',id:id}).then(function(){toast('Note withdrawn');nav('/recommendation',false);});}
function toggleWatch(e,tk){e.stopPropagation();var b=e.target,on=b.classList.contains('on'),act=on?'remove':'add';
 postJson('/api/watchlist',{action:act,ticker:tk}).then(function(){b.classList.toggle('on');b.textContent=on?'+ Watchlist':'✓ Watching';toast(tk+(on?' removed':' added'));});}
function doAdd(e){e.preventDefault();var v=document.getElementById('addtk').value.trim().toUpperCase();if(v)addWatch(v,function(){nav(here(),false);});return false;}
var _digest=null;
function digestRank(it){return it.earn?0:it.mat?1:it.kind==='filing'?2:3;}
function renderDigest(){var el=document.getElementById('digest');if(!el||!_digest)return;
 var fb=document.querySelector('.fbtn.on'),f=fb?fb.dataset.ff:'all';
 var items=_digest.filter(function(it){return f==='all'||it.kind===f});
 if(!items.length){el.innerHTML='<div class=fitem style="padding:14px 18px"><span class="fdesc muted">No recent '+(f==='all'?'filings or news':f==='filing'?'filings':'news')+'.</span></div>';return;}
 /* one row per ticker per day; expand for the individual items */
 var groups={},order=[];
 items.forEach(function(it){var k=it.tk+'|'+it.date;
  if(!groups[k]){groups[k]=[];order.push(k);}groups[k].push(it);});
 el.innerHTML=order.map(function(k){
  var g=groups[k].slice().sort(function(a,b){return digestRank(a)-digestRank(b)});
  var top=g[0],nf=0,nn=0;
  g.forEach(function(it){it.kind==='news'?nn++:nf++;});
  var badge=top.earn?'<span class="badge earn">Earnings</span>':(top.mat?'<span class="badge mat">8-K</span>':(top.kind==='news'?'<span class=badge>News</span>':'<span class=badge>'+esc(top.label)+'</span>'));
  var cnt=[];if(nf)cnt.push(nf+' filing'+(nf>1?'s':''));if(nn)cnt.push(nn+' news');
  var more=g.length>1?' <span class=fcount>'+cnt.join(' · ')+'</span>':'';
  var links=g.map(function(it){
   var tag=it.kind==='news'?(it.label||'News'):it.label;
   return '<a class=fnlink href="'+esc(it.url)+'" target=_blank rel=noopener><span class=fnsrc>'+esc(tag)+'</span>'+esc(it.desc)+'</a>';
  }).join('');
  return '<div class=fitem><div class=fhead><span class=fdate>'+esc(top.date)+'</span>'
   +'<a class=ftk data-tk="'+esc(top.tk)+'" href="/ticker/'+esc(top.tk)+'">'+esc(top.tk)+'</a>'+badge
   +'<span class=fdesc>'+esc(top.desc)+more+'</span></div>'
   +'<div class=fbody>'+(g.length===1&&top.summary?'<div class=fsum>'+esc(top.summary)+'</div>':'')+links+'</div></div>';
 }).join('');}
/* one batch fills both the bench table on /research and the held rows in the Book —
   any row that opts in with .nrow + the .p-* cells it wants */
function loadPerf(){var rows=document.querySelectorAll('.nrow[data-tk]');if(!rows.length)return;
 fetch('/api/perf').then(function(r){return r.json()}).then(function(p){
  rows.forEach(function(row){
   var d=p[row.dataset.tk];
   var px=row.querySelector('.p-price');
   if(!d){if(px)px.textContent='—';return;}
   if(px)px.textContent=fmtPrice(d.price);
   [['d1','.p-d1'],['m1','.p-m1'],['m6','.p-m6'],['ytd','.p-ytd']].forEach(function(k){
    var c=row.querySelector(k[1]),v=d[k[0]];if(!c)return;
    if(v==null){c.textContent='—';return;}
    c.textContent=(v>=0?'+':'−')+Math.abs(v).toFixed(1)+'%';c.className=c.className.replace(/ (up|down)\\b/g,'')+(v>=0?' up':' down');});
  });
 }).catch(function(){});}
function loadDigest(){var el=document.getElementById('digest');if(!el)return;
 fetch('/api/digest').then(function(r){return r.json()}).then(function(items){_digest=items||[];renderDigest();
 }).catch(function(){el.innerHTML='<div class=fitem style="padding:14px 18px"><span class="fdesc muted">Digest unavailable.</span></div>';});}
var _charts={};
/* Chart.js ships a 1000ms entry animation and we destroy+recreate on every range or
   mode click, so each click cost a full second of the line drawing itself in. That was
   the delay left after the CSS pass (David 2026-08-13: "still a delay with the buttons
   i'm not sure"). Charts are for reading, not for watching. */
if(typeof Chart!=='undefined'){Chart.defaults.animation=false;Chart.defaults.animations={};
 Chart.defaults.transitions.active.animation.duration=0;}
function cgrid(){return 'rgba(128,128,128,.12)';}
function cssvar(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
function fmtPrice(v){v=Number(v);if(!isFinite(v))return'';
 return Math.abs(v)>=10000?'$'+Math.round(v).toLocaleString():'$'+v.toFixed(2);}
/* in-chart value labels: last-value pill at the line's end + muted high/low markers.
   NB: the formatter must NOT live in chart options — Chart.js v4 treats option
   functions as scriptable and calls them with a context object (throws). */
var _fmtMap=new WeakMap();
var lineLabels={id:'lineLabels',afterDatasetsDraw:function(ch){
 var f=_fmtMap.get(ch.canvas);if(!f)return;
 var meta=ch.getDatasetMeta(0),pts=meta.data,data=ch.data.datasets[0].data;
 if(!pts||pts.length<2||!data||!data.length)return;
 var ctx=ch.ctx,area=ch.chartArea;
 var iMax=0,iMin=0;for(var i=1;i<data.length;i++){if(data[i]>data[iMax])iMax=i;if(data[i]<data[iMin])iMin=i;}
 ctx.save();
 function put(i,above){var p=pts[i];if(!p)return;
  ctx.font='500 10.5px Inter,sans-serif';ctx.textAlign='center';ctx.fillStyle=cssvar('--mut');
  var y=above?p.y-8:p.y+16;y=Math.max(area.top+10,Math.min(area.bottom-4,y));
  var x=Math.max(area.left+26,Math.min(area.right-26,p.x));ctx.fillText(f(data[i]),x,y);}
 if(iMax!==data.length-1)put(iMax,true);
 if(iMin!==data.length-1&&iMin!==iMax)put(iMin,false);
 var lp=pts[pts.length-1],lv=f(data[data.length-1]),col=ch.data.datasets[0].borderColor;
 ctx.font='600 11px Inter,sans-serif';
 var w=ctx.measureText(lv).width+14,h=20;
 var x=Math.min(lp.x+8,area.right-w),y=Math.max(area.top+h/2,Math.min(area.bottom-h/2,lp.y));
 ctx.fillStyle=col;ctx.beginPath();ctx.arc(lp.x,lp.y,3,0,7);ctx.fill();
 ctx.beginPath();
 if(ctx.roundRect)ctx.roundRect(x,y-h/2,w,h,10);else ctx.rect(x,y-h/2,w,h);
 ctx.fill();
 ctx.fillStyle='#fff';ctx.textAlign='center';ctx.fillText(lv,x+w/2,y+3.5);
 ctx.restore();}};
/* Price charts work like the portfolio chart now: ONE series is fetched per ticker and
   every range slices it client-side. Range clicks used to be a network round trip each
   (David 2026-08-13: "still a delay with the buttons"), and a per-range fetch could not
   report growth over the window either — which is what he actually wants to read. */
var _pxdata={};
function pxCut(d,range){
 var n=d.dates.length,today=d.dates[n-1],start;
 if(range==='ytd'){start=today.slice(0,4)+'-01-01';}
 else{var days={'1w':7,'1mo':30,'3mo':91,'6mo':182,'1y':365,'5y':4000}[range]||182;
  var t=new Date(today+'T00:00:00Z');t.setUTCDate(t.getUTCDate()-days);start=t.toISOString().slice(0,10);}
 var i=0;while(i<n&&d.dates[i]<start)i++;
 if(n-i<2)i=Math.max(0,n-2);
 return {dates:d.dates.slice(i),closes:d.closes.slice(i)};}
function pxSummary(box,cut,label){
 var el=box.querySelector('[data-pxsum]');if(!el||cut.closes.length<2)return;
 var a=cut.closes[0],b=cut.closes[cut.closes.length-1],chg=b-a,pct=a?chg/a*100:0,up=chg>=0;
 var hi=0,lo=0;
 cut.closes.forEach(function(v,i){if(v>cut.closes[hi])hi=i;if(v<cut.closes[lo])lo=i;});
 el.innerHTML='<span class="pfs '+(up?'up':'down')+'">'+(up?'+':'−')+'$'+Math.abs(chg).toFixed(2)
  +' ('+(up?'+':'−')+Math.abs(pct).toFixed(1)+'%)</span>'
  +'<span class=pfsl>'+esc(label)+'</span>'
  +'<span class=pfsl>'+esc(cut.dates[0])+' → '+esc(cut.dates[cut.dates.length-1])+'</span>'
  +'<span class=pfsl>High '+fmtPrice(cut.closes[hi])+' <i>'+esc(cut.dates[hi])+'</i></span>'
  +'<span class=pfsl>Low '+fmtPrice(cut.closes[lo])+' <i>'+esc(cut.dates[lo])+'</i></span>';}
function drawPriceChart(box,range,btn){var tk=box.dataset.ticker;
 box.querySelectorAll('.rbtn').forEach(function(b){b.classList.toggle('on',btn?b===btn:b.dataset.range===range);});
 box.dataset.pxrange=range;  /* recorded first, so a click during the load is honoured.
    NB: NOT data-range — that is the buttons' attribute, and putting it on the container
    made .pchart itself match every [data-range] selector on the page. */
 var d=_pxdata[tk];
 if(!d){loadPriceSeries(box);return;}
 if(!d.closes||d.closes.length<2)return;
 var cv=box.querySelector('canvas');if(!cv||typeof Chart==='undefined')return;
 var cut=pxCut(d,range),lbl=(box.querySelector('.rbtn.on')||{}).textContent||'';
 pxSummary(box,cut,lbl);
 if(_charts['px'+tk])_charts['px'+tk].destroy();
 var up=cut.closes[cut.closes.length-1]>=cut.closes[0],col=cssvar(up?'--pos':'--neg');
 _fmtMap.set(cv,fmtPrice);
 _charts['px'+tk]=new Chart(cv,{type:'line',plugins:[lineLabels],
  data:{labels:cut.dates,datasets:[{data:cut.closes,borderColor:col,backgroundColor:col+'1e',borderWidth:1.7,fill:true,pointRadius:0,tension:.06}]},
  options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
   layout:{padding:{top:14}},
   plugins:{legend:{display:false},tooltip:{displayColors:false,callbacks:{label:function(c){return fmtPrice(c.raw)}}}},
   scales:{y:{position:'right',grid:{color:cgrid()},ticks:{callback:function(v){return fmtPrice(v)}}},
    x:{ticks:{maxTicksLimit:6,maxRotation:0},grid:{display:false}}}}});}
function loadPriceSeries(box){var tk=box.dataset.ticker;
 if(box.dataset.loading)return;box.dataset.loading='1';
 fetch('/api/history?ticker='+encodeURIComponent(tk)+'&range=5y').then(function(r){return r.json()}).then(function(d){
  box.dataset.loading='';
  if(!d||!d.closes||!d.closes.length)return;
  _pxdata[tk]=d;drawPriceChart(box,box.dataset.pxrange||'6mo',null);
 }).catch(function(){box.dataset.loading='';});}
function initPriceCharts(){document.querySelectorAll('.pchart[data-ticker]').forEach(function(box){
 drawPriceChart(box,box.dataset.pxrange||'6mo',null);});}  /* range clicks ride the global .rbtn delegate */
var _spy=null,_spyLoading=false;
function loadSpy(){if(_spy||_spyLoading)return;_spyLoading=true;
 fetch('/api/history?ticker=SPY&range=2y').then(function(r){return r.json()}).then(function(d){
  _spy={};(d.dates||[]).forEach(function(dt,i){_spy[dt]=d.closes[i]});_spyLoading=false;
  if(_pfmode==='perf')drawPf();
 }).catch(function(){_spy={};_spyLoading=false;});}
var _pfdata=null,_pfmode='val',_pfrange='3M';
function pfSummary(cut,mode,key){var el=document.getElementById('pfsum');if(!el||cut.length<2)return;
 if(mode!=='val'){var base=cut[0][key],chg=cut[cut.length-1][key]-base,up=chg>=0;
  var hi=cut[0],lo=cut[0];
  cut.forEach(function(p){if(p[key]>hi[key])hi=p;if(p[key]<lo[key])lo=p;});
  var tag=mode==='hold'?'unrealized P&L on stocks & ETFs only · cost basis vs daily closes · excludes cash, money market and the note':'what you made · deposits, withdrawals & transfers removed · dashed = SPY with the same capital';
  var book=(mode==='hold'&&cut[cut.length-1].hv!=null)?'<span class=pfsl>book now '+money(cut[cut.length-1].hv)+'</span>':'';
  el.innerHTML='<span class="pfs '+(up?'up':'down')+'">'+signed(chg)+'</span>'
   +'<span class=pfsl>'+esc(cut[0].date)+' → '+esc(cut[cut.length-1].date)+'</span>'
   +'<span class=pfsl>High '+signed(hi[key]-base)+' <i>'+esc(hi.date)+'</i></span>'
   +'<span class=pfsl>Low '+signed(lo[key]-base)+' <i>'+esc(lo.date)+'</i></span>'
   +book+'<span class=pfsl>'+tag+'</span>';return;}
 var first=cut[0].value,lastv=cut[cut.length-1].value,chg=lastv-first,pct=first?chg/first*100:0,up=chg>=0;
 var hi=cut[0],lo=cut[0];
 cut.forEach(function(p){if(p.value>hi.value)hi=p;if(p.value<lo.value)lo=p;});
 el.innerHTML='<span class="pfs '+(up?'up':'down')+'">'+signed(chg)+' ('+(up?'+':'−')+Math.abs(pct).toFixed(1)+'%)</span>'
  +'<span class=pfsl>'+esc(cut[0].date)+' → '+esc(cut[cut.length-1].date)+'</span>'
  +'<span class=pfsl>High '+money(hi.value)+' <i>'+esc(hi.date)+'</i></span>'
  +'<span class=pfsl>Low '+money(lo.value)+' <i>'+esc(lo.date)+'</i></span>'
  /* Total value counts deposits, so a window containing a big transfer prints a growth
     number that is mostly money moving in, not money made. Name it rather than let the
     percentage lie by omission — "Return" is the flow-adjusted view. */
  +(function(){var pl0=cut[0].pl,pl1=cut[cut.length-1].pl;
    if(pl0==null||pl1==null)return '';var dep=chg-(pl1-pl0);
    return Math.abs(dep)>Math.max(1000,Math.abs(chg)*0.05)
      ? '<span class="pfsl warnl">'+signed(dep)+' of this is money moved in or out — see Return</span>' : '';})()
  +'<span class=pfsl>whole account · stocks &amp; ETFs + money market + cash + note · deposits count</span>';}
/* record the request FIRST: a click that lands before /api/pfhistory resolves must still
   be honoured when the data arrives (David 2026-08-13: "tabs not super receptive"). */
function drawPf(range){if(range)_pfrange=range;
 var cv=document.getElementById('pfchart');if(!cv||!_pfdata||!_pfdata.length)return;
 range=_pfrange;
 var mode=_pfmode,key=mode==='hold'?'hpl':mode==='perf'?'pl':'value';
 var data=_pfdata.filter(function(x){return x[key]!=null});
 if(data.length<2){mode='val';key='value';data=_pfdata.filter(function(x){return x.value!=null});}
 if(data.length<2)return;
 var cut;
 if(range==='YTD'){var jan=data[data.length-1].date.slice(0,4)+'-01-01',i=0;
  while(i<data.length&&data[i].date<jan)i++;cut=data.slice(Math.max(0,Math.min(i,data.length-2)));}
 else{var days={'1W':7,'1M':30,'3M':90,'6M':182,'ALL':1e9}[range]||1e9;
  cut=data.length>days?data.slice(data.length-days):data;}
 var rel=mode!=='val',base=rel?cut[0][key]:0;
 var series=cut.map(function(x){return rel?+(x[key]-base).toFixed(2):x[key]});
 pfSummary(cut,mode,key);
 if(_charts.pf)_charts.pf.destroy();var up=series[series.length-1]>=series[0],col=cssvar(up?'--pos':'--neg');
 var fmt=rel?signed:money;
 _fmtMap.set(cv,fmt);
 var dsets=[{data:series,borderColor:col,backgroundColor:col+'1e',borderWidth:1.9,fill:true,pointRadius:0,tension:.06}];
 if(mode==='perf'){loadSpy();
  if(_spy){var s0=null,spySeries=cut.map(function(x){var v=_spy[x.date];if(v!=null&&s0==null)s0=v;
    return (v!=null&&s0)?+(((v/s0)-1)*base).toFixed(2):null;});
   if(spySeries.some(function(v){return v!=null}))
    dsets.push({data:spySeries,borderColor:cssvar('--fade'),borderWidth:1.4,borderDash:[5,4],fill:false,pointRadius:0,tension:.06,spanGaps:true});}}
 _charts.pf=new Chart(cv,{type:'line',plugins:[lineLabels],
  data:{labels:cut.map(function(x){return x.date}),datasets:dsets},
  options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
   layout:{padding:{top:14}},
   plugins:{legend:{display:false},tooltip:{displayColors:false,callbacks:{label:function(c){return (c.datasetIndex===1?'SPY same-capital: ':'')+fmt(c.raw)}}}},
   scales:{y:{position:'right',grid:{color:cgrid()},ticks:{callback:function(v){return (rel&&v>0?'+':v<0?'−':'')+'$'+Math.abs(v/1000).toFixed(0)+'k'}}},
    x:{ticks:{maxTicksLimit:6,maxRotation:0},grid:{display:false}}}}});}
function loadPfChart(){var cv=document.getElementById('pfchart');if(!cv||typeof Chart==='undefined')return;
 /* the page just re-rendered, so adopt whatever the fresh markup says is selected —
    then any click during the fetch overrides it through the global .rbtn delegate. */
 var m0=document.querySelector('#pfmode .rbtn.on'),r0=document.querySelector('[data-pfrange].on');
 _pfmode=m0?m0.dataset.pfmode:'val';_pfrange=r0?r0.dataset.pfrange:'3M';
 fetch('/api/pfhistory').then(function(r){return r.json()}).then(function(d){
  if(!d||!d.length){cv.parentNode.innerHTML='<div class="muted" style="padding:22px">Portfolio history will build as your account syncs.</div>';return;}
  _pfdata=d;
  var last=d[d.length-1]||{},m=document.getElementById('pfmode');
  if(m){var has={val:true,perf:last.pl!=null,hold:last.hpl!=null};
   if(!has.perf&&!has.hold){m.style.display='none';_pfmode='val';}
   else m.querySelectorAll('[data-pfmode]').forEach(function(b){
     if(!has[b.dataset.pfmode]){b.style.display='none';if(_pfmode===b.dataset.pfmode)_pfmode='val';}});}
  drawPf();
 });}
function paginate(tbl,size){var tb=tbl.tBodies[0];if(!tb)return;
 var rows=Array.prototype.slice.call(tb.rows).filter(function(r){return !r.classList.contains('morerow')});
 if(rows.length<=size)return;var shown=size;
 var tr=document.createElement('tr');tr.className='morerow';var td=document.createElement('td');
 td.colSpan=(rows[0].cells.length||1);var btn=document.createElement('button');btn.className='morebtn';td.appendChild(btn);tr.appendChild(td);tb.appendChild(tr);
 function render(){for(var i=0;i<rows.length;i++)rows[i].style.display=i<shown?'':'none';
  btn.textContent='Show '+Math.min(size,rows.length-shown)+' more  ·  '+shown+' of '+rows.length;if(shown>=rows.length)tr.style.display='none';}
 btn.onclick=function(e){e.stopPropagation();e.preventDefault();shown=Math.min(shown+size,rows.length);render();};render();}
function paginateAll(){document.querySelectorAll('#main table').forEach(function(t){if(!t.dataset.pg){t.dataset.pg='1';paginate(t,10);}});}
var _pollIvs={};
function pollResearch(tk,box){
 if(_pollIvs['r'+tk])clearInterval(_pollIvs['r'+tk]);
 var iv=_pollIvs['r'+tk]=setInterval(function(){
  if(!document.body.contains(box)){clearInterval(iv);return;}
  fetch('/api/research/status?ticker='+encodeURIComponent(tk)).then(function(r){return r.json()}).then(function(s){
   if(s.status==='idle'){clearInterval(iv);box.style.display='none';return;}
   if(renderResearch(tk,s,box,false))clearInterval(iv);
  }).catch(function(){});},5000);}
/* One renderer for the research box. The bar is built from deliverables that exist
   on disk, so it reflects the run rather than the log's last prose line — and a run
   that stopped short reports what is missing instead of hiding itself. */
function rProgress(p){
 if(!p||!p.total)return '';
 var ticks=p.steps.map(function(st){
  return '<span class="rstep'+(st.done?' on':'')+'" title="'+esc(st.label)+' — '+(st.done?'written':'pending')+'"></span>';}).join('');
 var next=null;p.steps.forEach(function(st){if(!next&&!st.done)next=st.label;});
 return '<div class=rbar><div class=rbarfill style="width:'+p.pct+'%"></div></div>'
  +'<div class=rsteps>'+ticks+'</div>'
  +'<div class=rmeta>'+p.done+' of '+p.total+' deliverables'+(next?' · next: '+esc(next):'')+'</div>';}
function renderResearch(tk,s,box,poll){
 if(s.done){box.className='rstatus done';
  box.innerHTML='✓ Research complete — <a class=btn data-path="'+esc(s.link)+'" href="/view?path='+esc(s.link)+'">Open report</a>'+rProgress(s.progress);
  return true;}
 if(s.status==='error'||s.status==='incomplete'){var pr=s.progress||{};
  box.className='rstatus err';
  /* partial work is recoverable — always offer the way back in, never just the error */
  box.innerHTML='⚠ '+esc(s.msg||'Run stopped early')+rProgress(pr)
   +(pr.done?'<button class=btn onclick=\'genResearch(event,"'+tk+'")\'>Resume — finish the remaining '+(pr.total-pr.done)+'</button>':'');
  return true;}
 box.className='rstatus';
 if(s.status==='queued'){
  box.innerHTML='⏳ '+esc(tk)+' '+esc((s.msg||'queued').slice(0,120))+' <span class=rmsg>(one Claude session at a time)</span>'+rProgress(s.progress);
  if(poll)pollResearch(tk,box);
  return false;}
 box.innerHTML='<span class=spin></span> Researching '+esc(tk)+'…  <span class=rmsg>'+esc((s.msg||'running').slice(0,90))+'</span>'+rProgress(s.progress);
 if(poll)pollResearch(tk,box);
 return false;}
function checkResearch(){var box=document.getElementById('research-status');if(!box)return;var tk=box.dataset.ticker;
 fetch('/api/research/status?ticker='+encodeURIComponent(tk)).then(function(r){return r.json()}).then(function(s){
  if(s.status==='idle'){box.style.display='none';return;}box.style.display='';
  renderResearch(tk,s,box,true);
 }).catch(function(){});}
function boardRefresh(){toast('Refreshing candidate board…');post('/api/board/refresh').then(function(r){return r.json()}).then(function(d){toast(d.msg||'started');});}
var TX_LABEL={BUY:'Buy',SELL:'Sell',DIVIDEND:'Dividend',INTEREST:'Interest',CONTRIBUTION:'Deposit',
 WITHDRAWAL:'Withdrawal',TRANSFER:'Transfer',FEE:'Fee',REINVEST:'Reinvest',REI:'Reinvest'};
function loadTransactions(){var t=document.getElementById('txtable');if(!t)return;var tb=t.tBodies[0];
 fetch('/api/transactions').then(function(r){return r.json()}).then(function(rows){
  if(!rows||!rows.length){tb.innerHTML='<tr><td colspan=6 class=muted style="padding:16px 24px">No transactions.</td></tr>';return;}
  tb.innerHTML=rows.map(function(x){
   var ty=(x.type||'').toUpperCase();
   var lab=TX_LABEL[ty]||(ty?ty.charAt(0)+ty.slice(1).toLowerCase():'—');
   var pcls=ty==='BUY'?'buy':ty==='SELL'?'sell':(ty==='DIVIDEND'||ty==='INTEREST')?'div':'';
   var comp=x.listed&&x.symbol
    ?'<a class=tklink data-tk="'+esc(x.symbol)+'" href="/ticker/'+esc(x.symbol)+'">'+esc(x.symbol)+'</a>'+(x.name?'<div class=subname>'+esc(x.name)+'</div>':'')
    :'<span class=txname>'+esc(x.name||x.desc||x.symbol||'—')+'</span>';
   var n=x.amount!=null?Number(x.amount):null;
   var amt=n!=null?((n>0?'+':'')+'$'+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:0})):'';
   var acls=n!=null&&n>0?' up':'';if(n!=null&&n<0)amt='−$'+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:0});
   var pr=(x.price!=null&&x.price!=0)?fmtPrice(x.price):'';
   var un=(x.units!=null&&x.units!=0)?Number(x.units).toLocaleString():'';
   return '<tr><td class=txdate>'+esc(x.date)+'</td><td><span class="txpill '+pcls+'">'+esc(lab)+'</span></td><td>'+comp+
    '</td><td class=num>'+un+'</td><td class=num>'+pr+'</td><td class="num amt'+acls+'">'+amt+'</td></tr>';
  }).join('');paginate(t,10);
 }).catch(function(){});}
function agentSync(){toast('Syncing agentic account…');
 post('/api/agent/sync').then(function(r){return r.json()}).then(function(d){
  toast(d.msg||(d.ok?'Sync launched — refreshes in ~1 min':'failed'));
  if(d.ok)setTimeout(function(){if(location.pathname==='/agent')nav('/agent',false);},70000);
 }).catch(function(){toast('error');});}
function agentTrade(){toast('Launching decision session…');
 post('/api/agent/trade').then(function(r){return r.json()}).then(function(d){
  toast(d.msg||(d.ok?'Decision session running — check the journal in a few minutes':'failed'));
 }).catch(function(){toast('error');});}
function agentPause(){var r=prompt('Pause new orders — reason (only you can clear it):');if(!r)return;
 post('/api/agent/pause?reason='+encodeURIComponent(r)).then(function(x){return x.json()}).then(function(d){toast(d.msg||'paused');nav(location.pathname,false);}).catch(function(){toast('error');});}
function agentUnpause(){if(!confirm('Clear PAUSED and allow new orders?'))return;
 post('/api/agent/unpause').then(function(x){return x.json()}).then(function(d){toast(d.msg||'cleared');nav(location.pathname,false);}).catch(function(){toast('error');});}
function labMode(m){if(!confirm('Set the lab to '+m.toUpperCase()+'?'))return;
 post('/api/agent/mode?lab='+m).then(function(x){return x.json()}).then(function(d){toast(d.msg||m);nav(location.pathname,false);}).catch(function(){toast('error');});}
function agentFeeds(){toast('Refreshing intel feed…');
 post('/api/agent/feeds').then(function(r){return r.json()}).then(function(d){toast(d.msg||'started');
  if(d.ok)setTimeout(function(){if(location.pathname==='/agent')nav('/agent',false);},75000);
 }).catch(function(){toast('error');});}
function genRec(){toast('Generating recommendation…');
 post('/api/recommend').then(function(r){return r.json()}).then(function(d){
  var box=document.getElementById('rec-status');
  if(!d.ok){if(box){box.style.display='';box.className='rstatus err';box.textContent=d.msg||'Launch failed.';}else toast(d.msg||'failed');return;}
  if(box){box.style.display='';box.className='rstatus';box.innerHTML='<span class=spin></span> Reading the portfolio and writing today’s recommendation…';pollRec(box);}
 }).catch(function(){toast('error');});}
function pollRec(box){
 if(_pollIvs.rec)clearInterval(_pollIvs.rec);
 var iv=_pollIvs.rec=setInterval(function(){
  if(!document.body.contains(box)){clearInterval(iv);return;}
 fetch('/api/recommend/status').then(function(r){return r.json()}).then(function(s){
  if(s.done){clearInterval(iv);nav('/recommendation',false);}
  else if(s.status==='error'){clearInterval(iv);box.className='rstatus err';box.textContent=s.msg;}
  else if(s.status==='running'){box.innerHTML='<span class=spin></span> <span class=rmsg>'+esc((s.msg||'running').slice(0,110))+'</span>';}
  else{clearInterval(iv);box.style.display='none';}
 }).catch(function(){});},6000);}
function checkRec(){var box=document.getElementById('rec-status');if(!box)return;
 fetch('/api/recommend/status').then(function(r){return r.json()}).then(function(s){
  if(s.status==='running'){box.style.display='';box.className='rstatus';box.innerHTML='<span class=spin></span> <span class=rmsg>'+esc((s.msg||'running').slice(0,110))+'</span>';pollRec(box);}
  else if(s.status==='error'){box.style.display='';box.className='rstatus err';box.textContent=s.msg;}
 }).catch(function(){});}
function updResearch(e,tk){e.stopPropagation();toast('Updating '+tk+' research…');
 post('/api/research/update?ticker='+encodeURIComponent(tk)).then(function(r){return r.json()}).then(function(d){
  var box=document.getElementById('research-status');
  if(!d.ok){if(box){box.style.display='';box.className='rstatus err';box.textContent=d.msg||'failed';}else toast(d.msg||'failed');return;}
  if(box){box.style.display='';box.className='rstatus';box.innerHTML='<span class=spin></span> Updating '+esc(tk)+' — reading what’s new since the report…';
   if(_pollIvs['u'+tk])clearInterval(_pollIvs['u'+tk]);
   var iv=_pollIvs['u'+tk]=setInterval(function(){
    if(!document.body.contains(box)){clearInterval(iv);return;}
    fetch('/api/research/update/status?ticker='+encodeURIComponent(tk)).then(function(r){return r.json()}).then(function(s){
     if(s.done){clearInterval(iv);box.className='rstatus done';box.innerHTML='✓ Update written — <a class=btn data-path="'+esc(s.link)+'" href="/view?path='+esc(s.link)+'">Read what changed</a>';}
     else if(s.status==='error'){clearInterval(iv);box.className='rstatus err';box.textContent=s.msg;}
    }).catch(function(){});},6000);}
 }).catch(function(){toast('error');});}
function genResearch(e,tk){e.stopPropagation();toast('Launching research on '+tk+'…');
 post('/api/research?ticker='+encodeURIComponent(tk)).then(function(r){return r.json()}).then(function(d){toast(d.msg||'started');
  var box=document.getElementById('research-status');if(box){box.style.display='';box.className='rstatus';box.innerHTML='<span class=spin></span> Researching '+esc(tk)+'…  <span class=rmsg>starting</span>';pollResearch(tk,box);}}).catch(function(){toast('error');});}
function loadStats(){var g=document.querySelector('.statgrid[data-stats]');if(!g)return;
 fetch('/api/quote?ticker='+encodeURIComponent(g.dataset.stats)).then(function(r){return r.json()}).then(function(d){renderStats(g,d);}).catch(function(){});}
function renderStats(g,d){
  if(!d)return;
  function set(id,v,cls){var e=document.getElementById(id);if(e){e.textContent=v;if(cls)e.className='stv '+cls;}}
  if(d.prev)set('st-prev','$'+d.prev.toFixed(2));
  if(d.dlo&&d.dhi)set('st-day','$'+d.dlo.toFixed(2)+' – $'+d.dhi.toFixed(2));
  if(d.cap)set('st-cap',fmtCap(d.cap));
  var w=document.getElementById('st-52');
  if(w&&d.ylo&&d.yhi){w.querySelector('.rlo').textContent='$'+d.ylo.toFixed(2);w.querySelector('.rhi').textContent='$'+d.yhi.toFixed(2);
   if(d.price){var p=Math.min(1,Math.max(0,(d.price-d.ylo)/((d.yhi-d.ylo)||1)));w.querySelector('.rmark').style.left=(p*100).toFixed(1)+'%';}}
  var sh=parseFloat(g.dataset.shares)||0,cb=parseFloat(g.dataset.cost)||0;
  if(sh>0&&d.price){var mv=sh*d.price,gpl=mv-sh*cb,gp=cb?gpl/(sh*cb)*100:0;
   set('st-mv',money(mv));
   set('st-pnl',signed(gpl)+' ('+(gpl>=0?'+':'−')+Math.abs(gp).toFixed(1)+'%)',gpl>=0?'up':'down');}
  if(d.price)document.querySelectorAll('.pld[data-lvl]').forEach(function(el){
   var lvl=parseFloat(el.dataset.lvl),pct=(lvl-d.price)/d.price*100;
   el.textContent=(pct>=0?'+':'')+pct.toFixed(1)+'% away (now '+fmtPrice(d.price)+')';
   el.className='pld'+(Math.abs(pct)<2.5?' near':'');});}
function daysAway(iso,today){var a=Date.parse(iso+'T00:00:00Z'),b=Date.parse(today+'T00:00:00Z');
 if(!isFinite(a)||!isFinite(b))return'';var n=Math.round((a-b)/86400000);
 return n<=0?'today':n===1?'tomorrow':n<45?('in '+n+'d'):n<730?('in '+Math.round(n/30.4)+' mo'):('in '+(n/365).toFixed(1)+' yr');}
function loadCalendar(){var el=document.getElementById('calendar');if(!el)return;
 fetch('/api/calendar').then(function(r){return r.json()}).then(function(d){
  var items=(d&&d.events)||[],today=(d&&d.as_of)||'',und=(d&&d.undated)||[];
  /* "how far out" is the thing that makes a quiet calendar read as quiet rather
     than stale (David 2026-08-13: "coming up also seems outdated") */
  var foot=und.length?('<div class=fitem style="padding:12px 18px"><span class="fdesc muted">No date on the wire yet for '
   +esc(und.join(', '))+' — funds and thin coverage often have none.</span></div>'):'';
  if(!items.length){el.innerHTML='<div class=fitem style="padding:14px 18px"><span class="fdesc muted">Nothing dated on the horizon.</span></div>'+foot;return;}
  el.innerHTML=items.map(function(e){
   var tk=e.tk?'<a class=ftk data-tk="'+esc(e.tk)+'" href="/ticker/'+esc(e.tk)+'">'+esc(e.tk)+'</a>':'<span class=ftk></span>';
   var b=e.kind==='earnings'?(e.held?'<span class="badge earn">Earnings</span>':'<span class=badge>Earnings</span>')
     :e.kind==='catalyst'?'<span class="badge mat">Catalyst</span>':'<span class=badge>'+esc(e.kind||'')+'</span>';
   var away=today?' <span class=fcount>'+esc(daysAway(e.date,today))+'</span>':'';
   return '<div class=fitem><div class=fhead><span class=fdate>'+esc(e.date)+'</span>'+tk+b+'<span class=fdesc>'+esc(e.label||'')+away+'</span></div></div>';
  }).join('')+foot;
 }).catch(function(){});}
/* ---- journal ---- */
function jnAdd(e){e.preventDefault();var m=document.getElementById('jn-msg').value.trim();if(!m)return false;
 fetch('/api/journal/notes',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({msg:m,tk:document.getElementById('jn-tk').value})})
  .then(function(){toast('Noted');nav('/journal',false);}).catch(function(){toast('error');});return false;}
function jnDel(id){postJson('/api/journal/notes',{action:'remove',id:id}).then(function(){nav('/journal',false);});}
function jdAdd(e){e.preventDefault();
 var g=function(i){var el=document.getElementById(i);return el?el.value.trim():''};
 if(!g('jd-thesis')){toast('Thesis is required — that is the point');return false;}
 fetch('/api/journal/decisions',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({tk:g('jd-tk'),action:g('jd-action'),qty:g('jd-qty'),price:g('jd-price'),
   source:g('jd-source'),thesis:g('jd-thesis'),expect:g('jd-expect'),kill:g('jd-kill')})})
  .then(function(){toast('Decision logged');nav('/journal',false);}).catch(function(){toast('error');});return false;}
function jdClose(id){var o=prompt('Outcome — what happened, and was the PROCESS right regardless of result?');if(o==null)return;
 var s=prompt('Process score 1-5 (5 = would make the same call again with the same info)');
 fetch('/api/journal/decisions',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({close:true,id:id,outcome:o,score:parseInt(s,10)||null})})
  .then(function(){toast('Closed');nav('/journal',false);});}
function jdPrefill(tk,action,qty,price,date){
 var set=function(i,v){var el=document.getElementById(i);if(el)el.value=v;};
 set('jd-tk',tk);set('jd-action',action);set('jd-qty',qty);set('jd-price',price);set('jd-source','trade '+date);
 var th=document.getElementById('jd-thesis');if(th){th.focus();th.scrollIntoView({block:'center'});}}
function loadUnjournaled(){var el=document.getElementById('unj');if(!el)return;
 Promise.all([fetch('/api/transactions').then(function(r){return r.json()}),
              fetch('/api/journal/decisions').then(function(r){return r.json()})]).then(function(res){
  var tx=res[0]||[],decs=res[1]||[];
  var caught=tx.filter(function(x){
   if(!x.listed||!x.symbol)return false;
   var ty=(x.type||'').toUpperCase();if(ty!=='BUY'&&ty!=='SELL')return false;
   return !decs.some(function(d){return d.tk===x.symbol&&Math.abs(new Date(d.date)-new Date(x.date))<5*864e5;});
  }).slice(0,6);
  if(!caught.length){el.innerHTML='';return;}
  el.innerHTML='<div class=sec>Unjournaled trades — log why while you remember</div><div class=chips>'+caught.map(function(x){
   var ty=(x.type||'').toLowerCase();
   return '<a class=chip onclick="jdPrefill(\''+esc(x.symbol)+'\',\''+ty+'\',\''+(x.units||'')+'\',\''+(x.price||'')+'\',\''+esc(x.date)+'\')">'
    +esc(x.date)+' · '+ty+' '+esc(x.symbol)+(x.units?' ×'+x.units:'')+'</a>';}).join('')+'</div>';
 }).catch(function(){});}
function loadLots(){var el=document.getElementById('lots');if(!el)return;
 fetch('/api/lots?ticker='+encodeURIComponent(el.dataset.lots)).then(function(r){return r.json()}).then(function(rows){
  if(!rows||!rows.length){el.innerHTML='<p class=muted>No purchase activity returned by the brokerage feed (history may predate the AggregatorA connection — avg cost above is still authoritative).</p>';return;}
  var avg=parseFloat(el.dataset.cost)||0,hadTransfer=false;
  var h='<div class=tablewrap><table class=lotst><thead><tr><th>Date</th><th>Side</th><th class=num>Units</th><th class=num>Price</th><th class=num>Cost</th><th class=num>vs. avg</th><th>Long-term on</th></tr></thead><tbody>';
  rows.forEach(function(x){
   var amt=(x.price&&x.units)?money(Math.abs(x.units*x.price)):'';
   var d=(x.price&&avg)?(((x.price-avg)/avg*100)):null;
   var dv=d==null?'':((d>=0?'+':'')+d.toFixed(1)+'%');
   var lcls=x.type==='BUY'?'buy':x.type==='SELL'?'sell':x.type==='REINVEST'?'div':'';
   var llab=x.type==='TRANSFER'?'Transfer in':x.type.charAt(0)+x.type.slice(1).toLowerCase();
   var lt='';
   if(x.type==='BUY'||x.type==='REINVEST'){
    var ltd=new Date(x.date);ltd.setDate(ltd.getDate()+366);
    var days=Math.ceil((ltd-Date.now())/864e5);
    lt=days>0?ltd.toISOString().slice(0,10)+' <span class=hint2>('+days+'d)</span>':'<span class="chg up">long-term ✓</span>';
   }else if(x.type==='TRANSFER'){hadTransfer=true;lt='<span class=hint2>carries over †</span>';}
   h+='<tr><td class=txdate>'+esc(x.date)+'</td><td><span class="txpill '+lcls+'">'+esc(llab)+'</span></td>'
    +'<td class=num>'+Number(x.units).toLocaleString()+'</td><td class=num>'+(x.price?fmtPrice(x.price):'—')+'</td><td class=num>'+amt+'</td>'
    +'<td class="num '+(d==null?'':(d>=0?'chg up':'chg down'))+'">'+dv+'</td><td class=ltcell>'+lt+'</td></tr>';});
  el.innerHTML=h+'</tbody></table></div><p class="muted hint2">vs. avg compares each tranche to your current average cost. "Long-term on" = first day a sale of that lot is a long-term capital gain (informational, not tax advice).'
   +(hadTransfer?' † In-kind transfers keep the ORIGINAL purchase date from the sending broker — check those records; it may already be long-term.':'')+'</p>';
 }).catch(function(){el.innerHTML='<p class=muted>Lots unavailable.</p>';});}
/* sidebar % chips ride fillPositions' single batch — see batchQuotes */
function sortTable(t,ci,th){var tb=t.tBodies[0];if(!tb)return;
 var rows=Array.prototype.slice.call(tb.rows).filter(function(r){return !r.classList.contains('morerow')});
 var dir=th.dataset.dir==='asc'?'desc':'asc';
 Array.prototype.forEach.call(t.tHead.rows[0].cells,function(x){delete x.dataset.dir;var a=x.querySelector('.sarr');if(a)a.remove();});
 th.dataset.dir=dir;th.insertAdjacentHTML('beforeend','<span class=sarr>'+(dir==='asc'?'↑':'↓')+'</span>');
 function key(r){var c=r.cells[ci];if(!c)return '';var s=c.textContent.trim().replace(/[$,%+▲▼()]/g,'').replace(/−/g,'-');
  var n=parseFloat(s.replace(/,/g,''));return isNaN(n)?c.textContent.trim().toLowerCase():n;}
 rows.sort(function(a,b){var x=key(a),y=key(b);
  if(typeof x==='number'&&typeof y==='number')return dir==='asc'?x-y:y-x;
  return dir==='asc'?String(x).localeCompare(String(y)):String(y).localeCompare(String(x));});
 rows.forEach(function(r){r.style.display='';tb.appendChild(r);});
 var mr=tb.querySelector('.morerow');if(mr)mr.style.display='none';}
function initSort(){document.querySelectorAll('#main table.dt').forEach(function(t){
 if(t.dataset.srt||!t.tHead||!t.tHead.rows.length)return;t.dataset.srt='1';
 Array.prototype.forEach.call(t.tHead.rows[0].cells,function(th,ci){
  if(!th.textContent.trim())return;th.classList.add('sortable');th.title='Sort';
  th.addEventListener('click',function(){sortTable(t,ci,th);});});});}
var _tocObs=null;
function initToc(){if(_tocObs){_tocObs.disconnect();_tocObs=null;}
 var toc=document.querySelector('.doctoc');if(!toc||typeof IntersectionObserver==='undefined')return;
 var links={};toc.querySelectorAll('.tocl').forEach(function(a){links[(a.getAttribute('href')||'').slice(1)]=a;});
 _tocObs=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting&&links[en.target.id]){
  toc.querySelectorAll('.tocl').forEach(function(a){a.classList.remove('on')});links[en.target.id].classList.add('on');}});},
  {rootMargin:'0px 0px -72% 0px'});
 document.querySelectorAll('#main article h2[id],#main article h3[id]').forEach(function(h){if(links[h.id])_tocObs.observe(h);});}
function setTitle(){var h=document.querySelector('#main h1');
 document.title=(h?h.textContent.trim().replace(/\s+/g,' '):'Stocks')+' · Stocks';}
function restoreTab(){var h=location.hash.slice(1);if(!h)return;
 var t=document.querySelector('.tab[data-tab="'+h+'"]');
 if(t&&!t.classList.contains('on'))t.click();}
function enhance(){fmtTables();loadStats();loadSparks();fillPositions();loadLifetime();loadPerf();loadDigest();loadCalendar();initPriceCharts();loadPfChart();loadTransactions();loadLots();loadUnjournaled();checkResearch();checkRec();checkBrief();paginateAll();initSort();initToc();restoreTab();}
/* Seg bars are page loads, not pane toggles, so unlike the agent tabs they pay a round
   trip before anything can paint. Start that fetch on pointerdown/hover — ~100ms before
   the click on touch, longer on a mouse — and the click consumes a result that is
   already in flight. Entries are single-use and 8s-lived: a stale shell would show a
   stale cash line, which is worse than 4ms. */
var _pre={};
function prefetch(route){
 route=(route||'').split('#')[0];
 if(!route||_pre[route])return;
 /* the seg bar lives inside #main, so after a nav the cursor is sitting on the
    destination's own (now active) seg — without this it prefetches the page we are
    already looking at, every single time */
 if(route===location.pathname+location.search)return;
 var url=route+(route.indexOf('?')>=0?'&':'?')+'partial=1';
 _pre[route]={t:Date.now(),p:fetch(url).then(function(r){
   return {ver:r.headers.get('X-App-Ver'),text:r.text()}}).catch(function(){return null})};
 setTimeout(function(){delete _pre[route]},8000);}
document.addEventListener('pointerover',function(e){
 var el=e.target.closest('.pfseg[data-route],.leaf[data-route]');if(el)prefetch(el.dataset.route);});
document.addEventListener('pointerdown',function(e){
 var el=e.target.closest('[data-route]');if(el)prefetch(el.dataset.route);});
/* A deep link (/dip/options#ctek) lands ON its item: the #fragment comes off before ?partial=1
   is added (fetch drops a fragment, so '/dip/options#ctek?partial=1' fetched the FULL page into
   #main), rides the pushed URL, and after the swap the target is opened (a <details> fold) and
   scrolled to instead of the top. */
function goHash(hash){if(!hash||hash.length<2)return false;var id=decodeURIComponent(hash.slice(1));
 if(document.querySelector('.tab[data-tab="'+id.replace(/"/g,'')+'"]'))return false;  /* a tab: restoreTab() owns it */
 var el=document.getElementById(id);if(!el)return false;if(el.tagName==='DETAILS')el.open=true;
 var d=el.closest&&el.closest('details');if(d)d.open=true;el.scrollIntoView({block:'start'});return true;}
function nav(route,push){
 var hi=route.indexOf('#'),hash=hi>=0?route.slice(hi):'';if(hi>=0)route=route.slice(0,hi);
 var url=route+(route.indexOf('?')>=0?'&':'?')+'partial=1';
 function live(){return fetch(url).then(function(r){
   return {ver:r.headers.get('X-App-Ver'),text:r.text()}});}
 var hit=_pre[route];delete _pre[route];
 ((hit&&Date.now()-hit.t<8000)?hit.p:Promise.resolve(null))
 .then(function(x){return x||live()})
 .then(function(x){
  if(x.ver&&typeof APPV!=='undefined'&&x.ver!==APPV){location.href=route;return null;} /* server redeployed — full reload */
  return x.text;}).then(function(h){if(h==null)return;
  /* inline, so it beats the stylesheet — keep it in step with #main{animation} */
  var m=document.getElementById('main');m.innerHTML=h;m.style.animation='none';void m.offsetWidth;m.style.animation='fade .07s linear';
  /* a page module's own <script data-run> (charts on /dip/industries, 2026-09-20): innerHTML never executes scripts, so re-create them */
  m.querySelectorAll('script[data-run]').forEach(function(s){var n=document.createElement('script');n.textContent=s.textContent;s.parentNode.replaceChild(n,s);});
  readBook();setActive(route);
  if(push)history.pushState({route:route+hash},'',route+hash);
  var nv=document.getElementById('nav');if(nv)nv.checked=false;window.scrollTo(0,0);setTitle();enhance();
  if(hash)goHash(hash);
 });
}
/* ---- stale-tab self-heal (2026-08-31): nav() only version-checks on CLICK, so a tab
   left open on one page ran the morning's CSS all day (David's screenshots). On every
   return to the tab, ask the server its version; reload if it moved. HEAD = ~0 cost. */
var _verChk=0;
function checkVer(){var now=Date.now();if(now-_verChk<15000)return;_verChk=now;
 fetch(location.pathname+location.search,{method:'HEAD'}).then(function(r){
  var v=r.headers.get('X-App-Ver');
  if(v&&typeof APPV!=='undefined'&&v!==APPV)location.reload();}).catch(function(){});}
window.addEventListener('focus',checkVer);
document.addEventListener('visibilitychange',function(){if(!document.hidden)checkVer();});
/* ---- live data (2026-09-23; David: "for stocks i own ofcourse as real time as possible, but other
   stuff ... every 5 min during market hours and off-market don't need to reload anything").
   MKT is the server's market() plus the LIVE cadences — in the boot script, and as data-mkt on the
   Holdings list, which refreshSide() re-renders. Market open: what we own re-quotes every
   MKT.owned_s, everything on screen every MKT.other_s. At the bell the ticks stop; one last pass
   runs after the server's post-close pass (MKT.pending until then); then nothing until the next
   open, whose first tick refreshes everything. Only while the page is visible: a hidden tab runs
   no timer at all, and on its return whatever fell due runs once. In words: INTERACTIVITY.md,
   "Live data: what refreshes when". */
var _mktAt=Date.now(),_ownAt=Date.now(),_othAt=Date.now(),_liveTo=null,
 _mktRaw=(function(){var el=document.querySelector('[data-mkt]');return el?el.dataset.mkt:null;})();
/* only a data-mkt this page has not read yet is news: a sidebar the re-render failed to replace
   still carries the page-load state, and reading that at the bell kept MKT.open true all night */
function readMkt(){var el=document.querySelector('[data-mkt]'),raw=el&&el.dataset.mkt,m=null;
 if(!raw||raw===_mktRaw)return false;
 try{m=JSON.parse(raw);}catch(_){return false;}
 _mktRaw=raw;MKT=m;_mktAt=Date.now();return true;}
function liveRun(tier){var now=Date.now();_ownAt=now;if(tier==='all'){_othAt=now;loadPerf();}fillPositions(tier);}
function liveDue(){var due=_mktAt+MKT.next_s*1000;
 if(MKT.open)due=Math.min(due,_ownAt+MKT.owned_s*1000,_othAt+MKT.other_s*1000);
 return due-Date.now();}
function liveArm(){clearTimeout(_liveTo);_liveTo=null;
 if(document.visibilityState!=='visible')return;
 _liveTo=setTimeout(liveTick,Math.min(Math.max(liveDue(),1000),2147483000));}
function liveTick(){_liveTo=null;
 if(document.visibilityState!=='visible')return;
 var now=Date.now();
 if(now>=_mktAt+MKT.next_s*1000){   /* the bell, the settled close, or the open: re-read the state */
  refreshSide().then(function(ok){
   if(!ok||!readMkt()){           /* the re-render failed: ask again in a minute — and an open market's */
    if(MKT.open){MKT.open=false;MKT.pending=true;}   /* timer fell due at the bell, so its ticks stop */
    _mktAt=Date.now();MKT.next_s=60;}
   else if(!MKT.pending)liveRun('all');                /* the close's final pass, or the open's first */
   liveArm();});
  return;}
 if(MKT.open)liveRun(now-_othAt>=MKT.other_s*1000-500?'all':'owned');
 liveArm();}
document.addEventListener('visibilitychange',function(){
 if(document.visibilityState==='visible')liveArm();else{clearTimeout(_liveTo);_liveTo=null;}});
/* ---- search typeahead ---- */
var _sr=[],_si=-1,_sq=null,_searchGlobals=0;
function srender(){var box=document.getElementById('sresults');if(!box)return;
 if(!_sr.length){box.className='sresults';box.innerHTML='';return;}
 box.className='sresults show';
 box.innerHTML=_sr.map(function(x,i){
  var tags='';if(x.held)tags+='<span class="srtag h">Held</span>';else if(x.watch)tags+='<span class=srtag>Watching</span>';
  if(x.research)tags+='<span class="srtag r">Research</span>';
  return '<div class="sri'+(i===_si?' on':'')+'" data-si='+i+'><span class=srtk>'+esc(x.tk)+'</span><span class=srname>'+esc(x.name)+'</span>'+tags+'</div>';
 }).join('');}
function sgo(i){var q=document.getElementById('q');var x=_sr[i];
 var v=x?x.tk:q.value.trim().toUpperCase();_sr=[];_si=-1;srender();if(!v)return;
 q.value='';q.blur();nav('/ticker/'+encodeURIComponent(v),true);}
function initSearch(){var q=document.getElementById('q');if(!q)return;
 q.addEventListener('input',function(){var v=q.value.trim();clearTimeout(_sq);
  if(!v){_sr=[];_si=-1;srender();return;}
  _sq=setTimeout(function(){fetch('/api/search?q='+encodeURIComponent(v)).then(function(r){return r.json()}).then(function(res){
   if(q.value.trim()!==v)return;_sr=res||[];_si=_sr.length?0:-1;srender();});},140);});
 q.addEventListener('keydown',function(e){
  if(e.key==='ArrowDown'&&_sr.length){e.preventDefault();_si=(_si+1)%_sr.length;srender();}
  else if(e.key==='ArrowUp'&&_sr.length){e.preventDefault();_si=(_si-1+_sr.length)%_sr.length;srender();}
  else if(e.key==='Escape'){_sr=[];_si=-1;srender();q.blur();}});
 q.addEventListener('blur',function(){setTimeout(function(){_sr=[];_si=-1;srender();},180);});
 if(_searchGlobals)return;_searchGlobals=1;  /* re-bound on every sidebar refresh; these are document-wide, bind once */
 document.addEventListener('mousedown',function(e){var s=e.target.closest('.sri');if(s){e.preventDefault();sgo(parseInt(s.dataset.si,10));}});
 document.addEventListener('keydown',function(e){
  if(e.key==='/'&&!/input|textarea|select/i.test(e.target.tagName||'')){
   var qq=document.getElementById('q');if(qq){e.preventDefault();qq.focus();qq.select();}}});}
function doSearch(e){e.preventDefault();
 if(_si>=0&&_sr.length)sgo(_si);
 else{var v=document.getElementById('q').value.trim().toUpperCase();if(v){_sr=[];srender();nav('/ticker/'+encodeURIComponent(v),true);}}
 return false;}
document.addEventListener('click',function(e){var t=e.target.closest('.tab');if(t){
 t.parentNode.querySelectorAll('.tab').forEach(function(x){x.classList.toggle('on',x===t)});
 document.querySelectorAll('#main .pane').forEach(function(p){p.classList.toggle('on',p.id===t.dataset.tab);});
 /* pane state -> URL hash so browser-back and re-entry restore WHERE you were
    (David 2026-08-13: "back doesn't properly go back" — half of it was this) */
 if(t.dataset.tab){try{history.replaceState(history.state,'',location.pathname+location.search+'#'+t.dataset.tab);}catch(_){}}
 return;}
 var b=e.target.closest('.fbtn');if(b){b.parentNode.querySelectorAll('.fbtn').forEach(function(x){x.classList.toggle('on',x===b)});renderDigest();return;}
 /* chart range/mode buttons: delegated so they highlight INSTANTLY. They used to get
    their onclick inside the /api/pfhistory .then, so every click before that resolved
    was silently swallowed — the "buttons aren't receptive" lag. */
 var rb=e.target.closest('.rbtn');if(rb){
  rb.parentNode.querySelectorAll('.rbtn').forEach(function(x){x.classList.toggle('on',x===rb)});
  if(rb.dataset.pfmode){_pfmode=rb.dataset.pfmode;drawPf();}
  else if(rb.dataset.pfrange){drawPf(rb.dataset.pfrange);}
  else if(rb.dataset.range){var box=rb.closest('.pchart[data-ticker]');if(box)drawPriceChart(box,rb.dataset.range,rb);}
 }});
initSearch();
document.addEventListener('click',function(e){
 var el=e.target.closest('[data-path],[data-tk],[data-home],[data-route]');if(!el)return;e.preventDefault();
 if(el.hasAttribute('data-home'))nav('/',true);
 else if(el.hasAttribute('data-route'))nav(el.dataset.route,true);
 else if(el.hasAttribute('data-tk'))nav('/ticker/'+el.dataset.tk,true);
 else nav('/view?path='+encodeURIComponent(el.dataset.path),true);
});
document.addEventListener('click',function(e){var fh=e.target.closest('.fhead');if(fh)fh.parentNode.classList.toggle('open');});
window.addEventListener('popstate',function(e){var r=(e.state&&e.state.route)||(location.pathname+location.search);
 nav(r.replace('&partial=1','').replace('?partial=1',''),false);});
/* Exactly ONE nav item is lit for the current page (2026-09-22 redesign). navKey() is the
   route → item table, the same as app.nav_key() (tests/test_dashboard_nav.py runs both over
   every route): the Personal book owns /, its segs and the tickers it holds; DIP owns /dip*; the
   agent /agent*, its documents and the tickers only it holds (OWN, from ticker_owners()); a
   ticker no book holds, and /research*, dossiers, boards and system docs, are Research's;
   Learn /learn*, /primer and /lookups. On a ticker page the matching Holdings row is marked too. */
function navKey(route){
 var p=route.split('?')[0],q=route.split('?')[1]||'',v;
 function under(r){return p===r||p.indexOf(r+'/')===0;}
 if(under('/dip'))return 'dip';
 if(under('/agent'))return 'agent';
 if(under('/research')||p.indexOf('/company/')===0)return 'research';
 if(under('/learn')||p==='/primer'||p==='/lookups')return 'learn';
 if(p==='/advised')return 'paused';
 if(p==='/view'){v=(q.match(/(?:^|&)path=([^&#]*)/)||[])[1]||'';
  try{v=decodeURIComponent(v.replace(/\+/g,' '));}catch(_){}
  if(v.indexOf('_engine/agent')===0)return 'agent';
  if(v.indexOf('_engine/recommendations')===0||v.split('/').slice(0,2).indexOf('journal')>=0)return 'personal';
  return 'research';}
 if(p.indexOf('/ticker/')===0){var t=p.slice(8).split('/')[0].toUpperCase();
  return (typeof OWN!=='undefined'&&OWN[t])||'research';}
 if(p==='/'||p===''||p==='/today'||p==='/recommendation'||p==='/journal'||p==='/brokera')return 'personal';
 return '';}
function setActive(route){
 var side=document.querySelector('.side');if(!side)return;
 var k=navKey(route),m=route.match(/^\/ticker\/([A-Za-z0-9.\-]+)/),tk=m?m[1].toUpperCase():'';
 side.querySelectorAll('.ni').forEach(function(x){x.classList.toggle('on',!!k&&x.dataset.nav===k);});
 side.querySelectorAll('.hrow').forEach(function(x){x.classList.toggle('cur',!!tk&&x.dataset.tk===tk);});}
setActive(decodeURIComponent(location.pathname)+location.search);
readBook();enhance();if(location.hash)goHash(location.hash);
window.addEventListener('hashchange',function(){goHash(location.hash);});   /* an in-page #link opens its fold too */
autoSync();
liveArm();
"""

import agent_page  # /agent page + APIs for the BrokerB agentic account (kept in its own module)
agent_page.register(app, wrap)
import strategies_page  # /agent/strategies — definitions of undervalued / selling activity / thesis types (2026-09-16)
strategies_page.register(app, wrap)

import today_page  # /today — same-day read on the companies we own (own module; owns its JS/CSS)
today_page.register(app, wrap)
if getattr(today_page, "QUOTE", None) is None:
    today_page.QUOTE = _quote   # /today reads the warmed quote cache (LIVE), not a second Finnhub loop
CSS += today_page.CSS

import jpm_page  # /brokera — PM brief + decision desk for the BROKERA book (own module)
jpm_page.register(app, wrap)

import advised_page  # /advised — David's window on Justin's book (own module)
advised_page.register(app, wrap)

import dip_page  # /dip — the DIP Venture book (BROKERA entity account: Waffle stake + strategic cash; own module)
dip_page.register(app, wrap)

import judgements_page  # /dip/judgements — what the local models concluded, and their reasoning
judgements_page.register(app, wrap)

import lookups_page  # /lookups — HBS-library upload desk (Capital IQ / IBISWorld exports)
lookups_page.register(app, wrap)

import primer_page  # /primer — David's finance primer (terms + desk applications)
primer_page.register(app, wrap)
JS += today_page.JS

# /learn — guides built on live numbers from the desk, plus the glossary (/primer) and the library
# (/lookups) under one Learn bar (2026-09-22). Guarded: if the module is missing or broken the
# dashboard still starts, and the sidebar's Learn link falls back to the glossary.
try:
    import learn_page
except Exception as _ex:   # noqa: BLE001 — any import failure, not only ImportError, must not stop the app
    learn_page = None
    print(f"learn_page not loaded ({type(_ex).__name__}: {_ex}) — /learn is off; Learn links to /primer",
          file=sys.stderr)
if learn_page is not None:
    try:
        learn_page.register(app, wrap)
        CSS += learn_page.CSS
        JS += learn_page.JS
    except Exception as _ex:   # noqa: BLE001
        print(f"learn_page.register failed ({type(_ex).__name__}: {_ex}) — /learn is off", file=sys.stderr)
        learn_page = None

def _fresh(k, ttl):
    return _fresh_hit(k, _CACHE.get(k), ttl)

def _warm_ticker(t):
    """The EDGAR filing list, Finnhub news and next earnings date behind /ticker/<t>
    (2–5s together cold, measured 2026-09-04 03:59Z — David clicking between watchlist
    names late evening, when every 10–30 min TTL had long expired). Paced: only an
    expired entry is fetched, a Finnhub call first waits for room under the budget
    (_fh_wait) and is followed by a pause, because a fan-out over 16 names blew through
    Finnhub's 60/min cap and pushed every quote onto the Yahoo fallback (measured
    2026-09-04 04:17Z)."""
    cik = cik_of(t)
    if cik and not _fresh(f"edgar:{cik}", _ttl(LIVE["news_s"])):
        edgar_filings(cik)
    if not _fresh(f"news:{t}", _ttl(LIVE["news_s"])) and _fh_wait():
        finnhub_news(t); time.sleep(1.0)
    if not _fresh(f"earn:{t}", _ttl(LIVE["earn_s"])) and _fh_wait():
        next_earnings(t); time.sleep(1.0)

def _warm_yahoo(t):
    """The two yfinance calls a ticker page needs: the company profile (name, sector)
    and the 5y close series. Yahoo answers a burst with 429s and long backoffs, so these
    run one at a time with a pause after each real fetch."""
    if not _fresh(f"prof:{t}", _ttl(LIVE["slow_s"])):
        profile(t); time.sleep(1.5)
    if not _fresh(f"hist:{t}:5y", _ttl(LIVE["chart_s"])):
        _hist_data(t, "5y"); time.sleep(1.5)

def _warm_perf():
    cached("perf", _ttl(LIVE["other_s"]), _perf_data)   # 1y yfinance batch, ~5s cold (14s on the slow link)

# ---------- the warmers: the LIVE policy, run (2026-09-23) ----------
# Two threads. The PRICE warmer wakes every tick_s and fetches only what _quotes_due() names and
# the budget affords, so the owned names keep their beat whatever else is loading; the PANE warmer
# (perf, filings, news, the digest, profiles, charts, broker activity) walks slower, paced for
# Finnhub and Yahoo. Both ask market(): in market hours they keep the LIVE cadences; after the
# close each runs exactly one pass once the closing prints settle, then finds nothing due until
# the next open.

def _quote_due(tk, m, owned):
    """Whether the price warmer owes `tk` a fetch now (m = market()):
      market hours  owned once owned_every() has passed (to the nearest tick), anything else once
                    other_s − other_lead_s has
      off-market    only while its price predates the settle point: the one post-close pass (or,
                    after a start off-market, the boot pass) — then nothing until the open
    A name Finnhub refused, or nothing priced, waits out its backoff first (_quote_backoff)."""
    if m["now"] < _QRETRY.get(tk, 0):
        return False
    c = _QCACHE.get(tk)
    ts = c[0] if c else 0.0
    if m["open"]:
        every = owned_every() if tk in owned else LIVE["other_s"] - LIVE["other_lead_s"]
        return m["now"] - ts >= every - LIVE["tick_s"] / 2
    return bool(m["settled"]) and ts < m["settled"]

def _quotes_due(now=None):
    """Every tracked name the price warmer owes a fetch now, owned first."""
    m = market(now)
    owned, tracked = _sets()
    return [tk for tk in sorted(owned) + sorted(tracked - owned) if _quote_due(tk, m, owned)]

def _quotes_affordable(due, owned, m=None, pool=()):
    """(take, renew): the due names this tick pays for, and the names whose cap/52-week range it
    renews. Owned names always go — in market hours their re-quotes are the reservation fh_room()
    holds back; off-market they are charged like anything else. The rest take what is left, in
    order: one call per quote, then two per renewal that is due — the names priced this tick
    first, then (market hours only) any other name in `pool`, owned first. Renewals come last and
    leave the pane warmer its news share (one call per name per news_s): a range is good for hours,
    the news is not. A quote that does not fit waits for a later tick; a renewal that does not fit
    keeps the old range on the page — rather than a thread that sleeps while the owned names miss
    their beat."""
    m = m or market()
    room = fh_room(m)
    take = []
    for tk in due:
        if tk in owned:
            take.append(tk)
            room -= 0 if m["open"] else 1
        elif room >= 1:
            take.append(tk)
            room -= 1
    renew = set()
    keep = math.ceil(len(pool) * 60 / LIVE["news_s"]) if m["open"] else 0
    for tk in take + ([t for t in pool if t not in take] if m["open"] else []):
        if room < 2 + keep:
            break
        if _meta_due(tk):
            renew.add(tk)
            room -= 2
    return take, renew

def _warm_quote(t, renew=False):
    """One name, re-checked under its lock: a page may have just fetched it. `renew`: whether the
    budget paid for its cap/52-week range this tick (_quote_meta reads it off the thread)."""
    _WARMING.renew = renew
    with _cache_lock(f"q:{t}"):
        if _quote_due(t, market(), _sets()[0]):
            _quote_fetch(t)

def _warm_meta(t):
    """A cap/52-week renewal on its own, in market hours, for a name whose price is not due this
    tick: merged into the cached quote, which keeps its own age."""
    _WARMING.renew = True
    key = load_key("finnhub")
    d = _quote_meta(t, key) if key else {}
    if d:
        with _cache_lock(f"q:{t}"):
            c = _QCACHE.get(t)
            if c and c[1].get("price"):
                _QCACHE[t] = (c[0], {**c[1], **d})

def _warm_quotes_pass(now=None):
    m = market(now)
    owned, tracked = _sets()
    take, renew = _quotes_affordable(_quotes_due(now), owned, m, sorted(owned) + sorted(tracked - owned))
    jobs = [(_warm_quote, t, t in renew) for t in take] + [(_warm_meta, t) for t in renew if t not in take]
    if jobs:
        with ThreadPoolExecutor(min(16, len(jobs))) as ex:
            list(ex.map(lambda j: j[0](*j[1:]), jobs))
    return take

def _warm_panes_pass():
    """One pass of the pane warmer: the 1y perf table; per name (owned first) the filings, news and
    earnings date behind /ticker/<t>; the digest over them; the profile and 5y history; the
    broker's activity. Each is a cached() read that returns at once while fresh, and off-market
    _ttl() makes fresh mean "fetched since the post-close pass" — so after that pass a tick costs
    nothing until the open."""
    owned, tracked = _sets()
    tks = sorted(owned) + sorted(tracked - owned)
    _warm_perf()
    for t in tks:
        _warm_ticker(t)
        _warm_perf()            # a pass can outlast other_s; the table's prices keep their beat
    cached("digest", _ttl(LIVE["news_s"]), _digest_items)
    for t in tks:
        _warm_yahoo(t)
        _warm_perf()
    if not _fresh("st_activities:brokera", _ttl(LIVE["activity_s"])):
        st = _st()
        if st:
            _st_activities(st)   # transactions + lots: one AggregatorA fetch per activity_s

def _wlog(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} warm: {msg}", file=sys.stderr, flush=True)

def _warm_quotes():
    was_open = None
    while True:
        try:
            m = market()
            due = _warm_quotes_pass()
            # logged only where the policy turns: an off-market pass (there should be one per
            # close, plus a retry for any name Finnhub refused) and the first pass of a session
            if due and not m["open"]:
                _wlog(f"off-market pass — {len(due)} prices: {', '.join(due[:12])}{' …' if len(due) > 12 else ''}")
            elif m["open"] and was_open is False:
                _wlog(f"market open — first pass, {len(due)} prices")
            was_open = m["open"]
        except Exception as ex:   # noqa: BLE001 — the warmer must outlive any one bad tick
            _wlog(f"price warmer: {type(ex).__name__}: {ex}")
        time.sleep(LIVE["tick_s"])

def _warm_panes():
    while True:
        try:
            _warm_panes_pass()
        except Exception as ex:   # noqa: BLE001
            _wlog(f"pane warmer: {type(ex).__name__}: {ex}")
        time.sleep(LIVE["panes_tick_s"])

def start_warmers():
    """Both warm threads, once per process (serve.sh runs this file as __main__, which calls it)."""
    if any(th.name == "warm-quotes" for th in threading.enumerate()):
        return
    threading.Thread(target=_warm_quotes, name="warm-quotes", daemon=True).start()
    threading.Thread(target=_warm_panes, name="warm-panes", daemon=True).start()

if __name__ == "__main__":
    print(f"Stocks dashboard -> http://<host-ip>:{PORT}")
    start_warmers()
    app.run(host="0.0.0.0", port=PORT, debug=False)
