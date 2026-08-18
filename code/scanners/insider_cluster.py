#!/usr/bin/env python3
"""
Insider cluster-buying scanner — the multi-buyer fingerprint.

Finds issuers where MULTIPLE insiders made open-market PURCHASES (Form 4,
transaction code 'P') within a trailing window. Cluster buying (>=2 distinct
insiders buying together) is a high-signal, non-financial tell that management
sees value the market doesn't — exactly what flagged OmniAb (Foehr + Higgins +
two officers buying in the same window, near the lows).

Free data only (SEC EDGAR). Rate-limited to respect EDGAR's ~10 req/s policy.

Usage:
    python insider_cluster.py --days 30 --min-buyers 2
    python insider_cluster.py --days 3            # quick smoke test

Output: ranked markdown + parquet in _engine/candidate-boards/.
Size/sector enrichment (small-cap + tech/consumer/bio filter) is applied in a
later stage once the warehouse has market caps; v1 emits the raw cluster signal.
"""
import argparse
import datetime as dt
import io
import re
import sys
import time
from pathlib import Path

import requests
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edgar_identity import UA  # SEC contact identity, config-driven
ENGINE = Path(__file__).resolve().parent.parent
OUT = ENGINE / "candidate-boards"
SEC = "https://www.sec.gov"
DAILY = SEC + "/Archives/edgar/daily-index/{yr}/QTR{q}/form.{ymd}.idx"

import threading
_lock = threading.Lock()
_last = [0.0]
def _throttle(min_interval=0.11):
    # global rate gate (~9 req/s) shared across threads; sleep under the lock
    with _lock:
        dtm = time.monotonic() - _last[0]
        if dtm < min_interval:
            time.sleep(min_interval - dtm)
        _last[0] = time.monotonic()

def get(url, tries=3):
    for i in range(tries):
        _throttle()
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(0.5 * (i + 1))
    return None

def form4_paths_for_day(day):
    """Return list of full-submission .txt paths for Form 4 filings on `day`."""
    q = (day.month - 1) // 3 + 1
    url = DAILY.format(yr=day.year, q=q, ymd=day.strftime("%Y%m%d"))
    r = get(url)
    if r is None:
        return []
    paths = []
    for line in r.text.splitlines():
        # fixed-ish columns: Form Type  Company  CIK  Date Filed  File Name
        if not line.strip() or line.lstrip()[0:1].isalpha() is False:
            pass
        parts = line.split()
        if parts and parts[0] == "4" and line.strip().endswith(".txt"):
            paths.append(line.split()[-1])
    return paths

def cik_from_path(p):
    m = re.search(r"edgar/data/(\d+)/", p)
    return m.group(1) if m else None

_OWN_RE = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)

def parse_purchases(txt):
    """Yield dicts of open-market purchases (code 'P', acquired) from a Form 4 submission."""
    m = _OWN_RE.search(txt)
    if not m:
        return
    try:
        doc = etree.fromstring(m.group(0).encode("utf-8", "ignore"))
    except etree.XMLSyntaxError:
        return
    def txt_at(node, path):
        el = node.find(path)
        return el.text.strip() if el is not None and el.text else None
    issuer = doc.find("issuer")
    if issuer is None:
        return
    iname = txt_at(issuer, "issuerName")
    icik = txt_at(issuer, "issuerCik")
    isym = txt_at(issuer, "issuerTradingSymbol")
    owner = None
    ro = doc.find("reportingOwner")
    if ro is not None:
        owner = txt_at(ro, "reportingOwnerId/rptOwnerName")
    for tbl in ("nonDerivativeTable", "derivativeTable"):
        t = doc.find(tbl)
        if t is None:
            continue
        for tr in t.findall(tbl.replace("Table", "Transaction")):
            code = txt_at(tr, "transactionCoding/transactionCode")
            ad = txt_at(tr, "transactionAmounts/transactionAcquiredDisposedCode/value")
            if code == "P" and ad == "A":
                try:
                    sh = float(txt_at(tr, "transactionAmounts/transactionShares/value") or 0)
                    px = float(txt_at(tr, "transactionAmounts/transactionPricePerShare/value") or 0)
                except (TypeError, ValueError):
                    sh, px = 0, 0
                yield {"issuer": iname, "cik": icik, "symbol": isym,
                       "owner": owner, "value": sh * px}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-buyers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="cap Form 4 fetches (smoke test)")
    args = ap.parse_args()

    from concurrent.futures import ThreadPoolExecutor
    today = dt.date.today()
    days = [today - dt.timedelta(d) for d in range(args.days)]

    # Phase A (cheap): collect all Form-4 submission paths in the window
    print(f"[insider_cluster] collecting Form 4 index over {args.days} days...", file=sys.stderr)
    all_paths = []
    for day in days:
        if day.weekday() >= 5:
            continue
        all_paths += form4_paths_for_day(day)

    # Phase B: pre-filter to issuers with >= min_buyers Form-4 filings.
    # A cluster of N distinct buyers requires >= N separate Form 4s, so issuers
    # below that threshold can't cluster and are skipped — cuts fetches sharply.
    by_cik = {}
    for p in all_paths:
        c = cik_from_path(p)
        if c:
            by_cik.setdefault(c, []).append(p)
    cand_paths = [p for c, ps in by_cik.items() if len(ps) >= args.min_buyers for p in ps]
    if args.limit:
        cand_paths = cand_paths[:args.limit]
    print(f"[insider_cluster] {len(all_paths)} form-4s / {len(by_cik)} issuers; "
          f"parsing {len(cand_paths)} filings from {sum(1 for ps in by_cik.values() if len(ps) >= args.min_buyers)} "
          f"multi-filer issuers...", file=sys.stderr)

    # Phase C: fetch + parse candidate filings (threaded, globally rate-limited)
    agg = {}
    n_forms = 0
    n_buys = 0
    def fetch_parse(p):
        r = get(SEC + "/Archives/" + p)
        return list(parse_purchases(r.text)) if r else []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for buys in ex.map(fetch_parse, cand_paths):
            n_forms += 1
            for buy in buys:
                key = buy["cik"]
                if not key:
                    continue
                n_buys += 1
                a = agg.setdefault(key, {"owners": set(), "value": 0.0,
                                         "symbol": buy["symbol"], "name": buy["issuer"]})
                if buy["owner"]:
                    a["owners"].add(buy["owner"])
                a["value"] += buy["value"]
            if n_forms % 1000 == 0:
                print(f"  parsed {n_forms}/{len(cand_paths)}...", file=sys.stderr)

    rows = [{"cik": k, "symbol": v["symbol"], "issuer": v["name"],
             "n_buyers": len(v["owners"]), "buy_value": round(v["value"]),
             "buyers": ", ".join(sorted(v["owners"]))}
            for k, v in agg.items() if len(v["owners"]) >= args.min_buyers]
    rows.sort(key=lambda x: (x["n_buyers"], x["buy_value"]), reverse=True)

    OUT.mkdir(exist_ok=True)
    stamp = today.strftime("%Y-%m-%d")
    import csv
    csvp = OUT / f"insider-cluster_{stamp}.csv"
    with csvp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cik", "symbol", "issuer", "n_buyers", "buy_value", "buyers"])
        w.writeheader()
        w.writerows(rows)
    md = OUT / f"insider-cluster_{stamp}.md"
    with md.open("w") as f:
        f.write(f"# Insider cluster-buying scan — {stamp}\n\n")
        f.write(f"Window: last {args.days} days · min {args.min_buyers} distinct insider buyers · "
                f"{n_forms} Form 4s scanned · {len(rows)} clustered issuers.\n\n")
        f.write("| Symbol | Issuer | # buyers | Open-mkt $ | Buyers |\n|---|---|--:|--:|---|\n")
        for r in rows[:60]:
            f.write(f"| {r['symbol'] or '—'} | {r['issuer'][:40]} | {r['n_buyers']} | "
                    f"${r['buy_value']:,} | {r['buyers'][:80]} |\n")
    print(f"\n[insider_cluster] {n_forms} forms, {n_buys} open-market buys parsed, "
          f"{len(rows)} clustered issuers -> {md}", file=sys.stderr)
    for r in rows[:15]:
        print(f"  {r['n_buyers']}x  {r['symbol'] or '?':6}  ${r['buy_value']:>12,}  {r['issuer'][:36]}")

if __name__ == "__main__":
    main()
