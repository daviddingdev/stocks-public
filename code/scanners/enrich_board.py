#!/usr/bin/env python3
"""
Enrich a raw scanner hit-list into a decision-ready candidate board.

Reads a scanner CSV (default: latest insider-cluster_*.csv), enriches each
ticker with market cap / sector / exchange / country via yfinance, then filters
to the mandate: small/micro-cap + international ADRs, tech/consumer/bio-leaning,
dropping large-caps, shells, and obvious junk. Writes a ranked markdown board.

Manual, on-demand — run when David asks for a candidate board. No scheduling.

Usage:
    python enrich_board.py                       # newest insider-cluster CSV
    python enrich_board.py --csv <path> --max-cap 5e9
"""
import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

import yfinance as yf

ENGINE = Path(__file__).resolve().parent.parent
BOARDS = ENGINE / "candidate-boards"

SECTOR_FIT = {"technology", "communication services", "healthcare",
              "consumer cyclical", "consumer defensive"}

def latest_csv(pattern="insider-cluster_*.csv"):
    files = sorted(BOARDS.glob(pattern))
    return files[-1] if files else None

def enrich(symbol):
    """Return dict of fundamentals, or None if not resolvable."""
    if not symbol:
        return None
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    if not info or info.get("quoteType") not in ("EQUITY", None):
        return None
    return {
        "mkt_cap": info.get("marketCap"),
        "sector": (info.get("sector") or "").strip(),
        "industry": (info.get("industry") or "").strip(),
        "exchange": info.get("exchange") or "",
        "country": info.get("country") or "",
        "adr": bool(info.get("country") and info.get("country") != "United States"),
        "name": info.get("shortName") or "",
        "price": info.get("currentPrice") or info.get("previousClose"),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=str, default="")
    ap.add_argument("--max-cap", type=float, default=5e9, help="drop caps above this (default 5e9)")
    ap.add_argument("--min-cap", type=float, default=2e7, help="drop micro shells below this (default 2e7)")
    ap.add_argument("--top", type=int, default=40, help="how many raw hits to enrich")
    args = ap.parse_args()

    src = Path(args.csv) if args.csv else latest_csv()
    if not src or not src.exists():
        print("No scanner CSV found. Run a scanner first (e.g. insider_cluster.py).", file=sys.stderr)
        sys.exit(1)
    rows = list(csv.DictReader(src.open()))
    print(f"[enrich_board] {len(rows)} raw hits from {src.name}; enriching top {args.top}...", file=sys.stderr)

    board = []
    for r in rows[:args.top]:
        e = enrich(r.get("symbol"))
        if not e or not e["mkt_cap"]:
            continue
        cap = e["mkt_cap"]
        if cap > args.max_cap or cap < args.min_cap:
            continue
        if e["sector"].lower() not in SECTOR_FIT:
            continue  # keep tech/consumer/bio/comm-services; drop financials/energy/industrials/etc.
        board.append({**r, **e, "cap": cap})
        print(f"  kept {e['name'][:28]:28} {r['symbol']:6} ${cap/1e6:,.0f}M {e['sector']}", file=sys.stderr)

    board.sort(key=lambda x: (int(x["n_buyers"]), x["cap"] and -x["cap"]), reverse=True)

    stamp = dt.date.today().strftime("%Y-%m-%d")
    out = BOARDS / f"board_{stamp}.md"
    with out.open("w") as f:
        f.write(f"# Candidate board — {stamp}\n\n")
        f.write(f"Source: {src.name} · filtered to ${args.min_cap/1e6:.0f}M–${args.max_cap/1e9:.0f}B, "
                f"tech/consumer/bio, US small-cap + international ADRs. {len(board)} names.\n\n")
        f.write("| Symbol | Name | Mkt cap | Sector | Country | Signal | Buyers |\n")
        f.write("|---|---|--:|---|---|---|---|\n")
        for b in board:
            adr = " (ADR)" if b["adr"] else ""
            f.write(f"| {b['symbol']} | {b['name'][:26]} | ${b['cap']/1e6:,.0f}M | "
                    f"{b['sector']} | {b['country'] or '—'}{adr} | "
                    f"{b['n_buyers']} insiders, ${int(b['buy_value']):,} | {b['buyers'][:60]} |\n")
        f.write("\n_Next step: pick at most 1–2 for the deep-research engine "
                "(see research/RUNBOOK.md). Concentrated — quality over volume._\n")
    print(f"\n[enrich_board] {len(board)} on the board -> {out}", file=sys.stderr)

if __name__ == "__main__":
    main()
