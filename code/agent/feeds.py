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


def universe():
    """Agent universe = its own watchlist + its own positions. INDEPENDENCE
    (David, 2026-08-12): BROKERA holdings are no longer merged in — this book is
    its own fund; BROKERA looks at it, not the other way around. (triggers.py
    still watches BROKERA names for David's phone via its own direct queries.)"""
    f = HERE / "universe.txt"
    if not f.exists():
        return []
    base = [l.strip().upper() for l in f.read_text().splitlines()
            if l.strip() and not l.startswith("#")]
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
def cik_map():
    """ticker -> zero-padded CIK, cached a week."""
    cache = DATA / "cik_map.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        return json.loads(cache.read_text())
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=UA, timeout=30)
        m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}
    except Exception:
        return json.loads(cache.read_text()) if cache.exists() else {}
    cache.write_text(json.dumps(m))
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
            rows.append({"date": date, "form": form, "items": items,
                         "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"})
        out[tk] = rows[:15]
        time.sleep(0.15)
    return out


# ---------- special-situations radar (market-wide, not universe-bound) ----------
# Sourcing doctrine: _engine/research/SOURCING.md — mechanism-driven channels.
# SC 13D = fresh activist/concentrated stakes · 10-12B = spinoff registrations ·
# Form 25 = delistings (forced-selling flag). Parsed from EDGAR daily form indices.
# NB: EDGAR's 13D/G modernization (Dec 2024) renamed the index form to "SCHEDULE 13D";
# "SC 13D" kept for any legacy stragglers. Amendments (/A) are excluded on purpose —
# we want NEW stakes, not position updates.
RADAR_FORMS = {"SCHEDULE 13D": "sc13d", "SC 13D": "sc13d",
               "10-12B": "spins", "25": "delistings", "25-NSE": "delistings"}


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


def special_situations(days=10):
    ticker_by_cik = {int(c): t for t, c in cik_map().items()}
    out = {"sc13d": [], "spins": [], "delistings": []}
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
    for k in out:
        out[k] = sorted(out[k], key=lambda x: x["date"], reverse=True)[:60]
    out["sc13d"] = _resolve_13d_subjects(out["sc13d"])
    out["spins"] = _resolve_spin_parents(out["spins"])
    return out


def _tag_held(situations, held):
    """A 13D/spin/delisting on a name we hold or watch is not one of dozens of
    market-wide rows, it is a tripwire on our own book — mark it so a reader (or a
    future scorer) does not have to cross-reference by hand (feeds.py-011: a TLS
    Schedule 13D sat unflagged in this exact radar). sc13d uses subject_ticker (the
    daily index's own 'ticker' field is the FILER's, resolved separately); spins and
    delistings key off the issuer's own CIK, so 'ticker' is already the subject."""
    held_hits = []
    for r in situations.get("sc13d") or []:
        tk = r.get("subject_ticker")
        r["held"] = bool(tk and tk in held)
        if r["held"]:
            held_hits.append({"kind": "sc13d", "ticker": tk, "date": r.get("date"), "url": r.get("url")})
    for key in ("spins", "delistings"):
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
          f"radar: {len(sit['sc13d'])} 13Ds, {len(sit['spins'])} spins, {len(sit['delistings'])} delistings"
          + (f" ({n_held_hits} on held/universe names)" if n_held_hits else "")
          + (f"  ⚠ DEGRADED (carried over): {', '.join(degraded)}" if degraded else "")
          + (f"  [vendor: {'; '.join(FH_FAILS[-3:])}]" if FH_FAILS else ""))


if __name__ == "__main__":
    if sys.argv[1:2] == ["refresh"]:
        refresh()
    else:
        sys.exit("usage: feeds.py refresh")
