#!/usr/bin/env python3
"""Filing pre-digestion — local model, zero Claude tokens.

ADDITIVE tool (2026-08-08, Mission Control local-models initiative). Summarizes a
ticker's recent SEC filings locally so Claude teardown/digest sessions read a tight
brief instead of raw filings (cuts their token load).

Usage:  python3 predigest.py TICKER [--max 5]
Output: _engine/research/predigest/<TICKER>_<date>.md
"""
import json, os, re, sys, time, urllib.request

HOME = os.path.expanduser("~")
sys.path.insert(0, f"{HOME}/maintenance/bin")
from localllm import ask, DEFAULT_MODEL

BASE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(BASE, "..", "agent", "data", "feed.json")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # SEC contact identity, config-driven


def fetch_text(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        raw = r.read().decode(errors="replace")
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def main():
    ticker = sys.argv[1].upper()
    mx = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 5
    filings = (json.load(open(FEED)).get("filings") or {}).get(ticker, [])[:mx]
    if not filings:
        print(f"no filings for {ticker} in feed.json")
        return
    parts = []
    for f in filings:
        label = f"{f.get('form', '?')} filed {f.get('date', '?')}"
        try:
            txt = fetch_text(f["url"])[:24000]
            time.sleep(0.5)   # SEC politeness
            s = ask(
                f"Summarize this SEC filing ({label}) for {ticker} in 3-6 bullet points. "
                "Only substance: numbers, changes, named parties, risks. If it's a routine "
                "Form 4 (insider trade), one bullet: who, bought/sold, how much. No fluff.\n\n" + txt,
                num_predict=350)
        except Exception as e:
            s = f"(fetch/summarize failed: {e})"
        parts.append(f"## {label}\n{f.get('url','')}\n\n{s}\n")
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
