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


INTERESTING = {"8-K", "10-K", "10-Q", "4", "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A", "S-1", "424B5"}


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


def _resolve_13d_subjects(rows, cap=20):
    """The daily form index lists the FILER (Coliseum Capital), not the SUBJECT
    (Sonos) — which made most 13Ds invisible without hand-resolution (the PM had
    to do it manually for SONO, 2026-08-13). The full-submission header carries a
    SUBJECT COMPANY block with its CIK: fetch it once per accession, cache forever."""
    cache_f = DATA / "sc13d_subjects.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        cache = {}
    t_by_cik = {int(c): t for t, c in cik_map().items()}
    fetched = 0
    for r in rows:
        acc = r["url"].rsplit("/", 1)[-1]
        if acc in cache:
            r.update(cache[acc])
            continue
        if fetched >= cap:  # politeness: resolve the backlog across successive runs
            continue
        entry = {}
        try:
            head = requests.get(r["url"], headers=UA, timeout=30).text[:6000]
            fetched += 1
            time.sleep(0.15)
            m = re.search(r"SUBJECT COMPANY:.*?COMPANY CONFORMED NAME:\s*(.+?)\n.*?CENTRAL INDEX KEY:\s*(\d+)",
                          head, re.S)
            if m:
                subj_cik = int(m.group(2))
                entry = {"subject": m.group(1).strip()[:60],
                         "subject_ticker": t_by_cik.get(subj_cik)}
        except Exception:
            pass
        cache[acc] = entry
        r.update(entry)
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
    return out


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
        "situations": special_situations(),
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
    print(f"feed.json: {len(tks)} tickers · {n_news} news · {n_fil} filings · "
          f"{len(feed['earnings'])} earnings · {len(feed['market_news'])} market headlines · "
          f"radar: {len(sit['sc13d'])} 13Ds, {len(sit['spins'])} spins, {len(sit['delistings'])} delistings"
          + (f"  ⚠ DEGRADED (carried over): {', '.join(degraded)}" if degraded else "")
          + (f"  [vendor: {'; '.join(FH_FAILS[-3:])}]" if FH_FAILS else ""))


if __name__ == "__main__":
    if sys.argv[1:2] == ["refresh"]:
        refresh()
    else:
        sys.exit("usage: feeds.py refresh")
