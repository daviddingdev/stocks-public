#!/usr/bin/env python3
"""Filing pre-digestion — local model, zero Claude tokens.

ADDITIVE tool (2026-08-08, Mission Control local-models initiative). Summarizes a
ticker's recent SEC filings locally so Claude teardown/digest sessions read a tight
brief instead of raw filings (cuts their token load).

Usage:  python3 predigest.py TICKER [--max 5]
Output: _engine/research/predigest/<TICKER>_<date>.md
"""
import json, os, re, sys, time, urllib.request
from pathlib import Path

HOME = os.path.expanduser("~")
sys.path.insert(0, f"{HOME}/maintenance/bin")
from localllm import ask, DEFAULT_MODEL

BASE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(BASE, "..", "agent", "data", "feed.json")
NAMES = os.path.join(BASE, "..", "agent", "names")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # SEC contact identity, config-driven


def fetch_text(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        raw = r.read().decode(errors="replace")
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def shelf_filings(ticker, mx):
    """Fallback when feed.json has no filings: the ticker's own names/<TK>/filings/
    shelf, already text-stripped by dossier.py. Newest first, by filename date."""
    d = Path(NAMES) / ticker / "filings"
    if not d.is_dir():
        return []
    return sorted(d.glob("*.txt"), reverse=True)[:mx]


def main():
    ticker = sys.argv[1].upper()
    mx = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 5
    filings = (json.load(open(FEED)).get("filings") or {}).get(ticker, [])[:mx]
    local = not filings
    if local:
        filings = shelf_filings(ticker, mx)
    if not filings:
        print(f"no filings for {ticker} in feed.json or names/{ticker}/filings")
        return
    parts = []
    for f in filings:
        if local:
            date, _, form = f.stem.partition("_")
            label = f"{form or '?'} filed {date}"
            src = str(f)
        else:
            label = f"{f.get('form', '?')} filed {f.get('date', '?')}"
            src = f.get("url", "")
        try:
            txt = f.read_text(errors="replace")[:24000] if local else fetch_text(f["url"])[:24000]
            if not local:
                time.sleep(0.5)   # SEC politeness
            s = ask(
                f"Summarize this SEC filing ({label}) for {ticker} in 3-6 bullet points. "
                "Only substance: numbers, changes, named parties, risks. If it's a routine "
                "Form 4 (insider trade), one bullet: who, bought/sold, how much. No fluff.\n\n" + txt,
                num_predict=350)
        except Exception as e:
            s = f"(fetch/summarize failed: {e})"
        parts.append(f"## {label}\n{src}\n\n{s}\n")
        print(f"done: {label}")
    outdir = os.path.join(BASE, "predigest")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{ticker}_{time.strftime('%Y-%m-%d')}.md")
    open(out, "w").write(
        f"# {ticker} — filing pre-digest {time.strftime('%Y-%m-%d')}\n"
        f"_Generated locally ({DEFAULT_MODEL}), for Claude sessions to read instead of raw filings._\n\n"
        + "\n".join(parts))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
