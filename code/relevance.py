#!/usr/bin/env python3
"""Local-model relevance scorer for the intel feed — zero Claude tokens.

ADDITIVE module (2026-08-08, Mission Control local-models initiative): reads
data/feed.json (produced by feeds.py — NOT modified here) and writes
data/feed_scored.json with a 0-10 materiality score + one-line reason per news item.
The dashboard/agent can adopt the scores whenever the owning session wants.
Runs via cron after market close; model: localllm.DEFAULT_MODEL via ollama (think:false)."""
import hashlib, json, os, subprocess, sys, time

HOME = os.path.expanduser("~")
sys.path.insert(0, f"{HOME}/maintenance/bin")
from localllm import ask_json, DEFAULT_MODEL

BASE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(BASE, "data", "feed.json")
OUT = os.path.join(BASE, "data", "feed_scored.json")
FRESH_H = 30            # score items from the last ~30h
BATCH = 15


def main():
    feed = json.load(open(FEED))
    cutoff = time.time() - FRESH_H * 3600
    items = []
    for ticker, arts in (feed.get("news") or {}).items():
        for a in arts or []:
            if a.get("datetime", 0) >= cutoff and a.get("headline"):
                items.append({"id": hashlib.md5((a.get("url") or a["headline"]).encode()).hexdigest()[:12],
                              "ticker": ticker, "headline": a["headline"][:160],
                              "summary": (a.get("summary") or "")[:220],
                              "datetime": a.get("datetime"), "url": a.get("url", "")})
    prev = {}
    try:
        prev = json.load(open(OUT)).get("scores", {})
    except Exception:
        pass
    todo = [i for i in items if i["id"] not in prev]
    scores = dict(prev)
    for b in range(0, len(todo), BATCH):
        batch = todo[b:b + BATCH]
        listing = "\n".join(f'{i["id"]} | {i["ticker"]} | {i["headline"]} | {i["summary"][:120]}'
                            for i in batch)
        v = ask_json(
            "Score each news item for an investor holding/watching these exact tickers. "
            "materiality 0-10: 0-2 = noise/PR/listicles, 3-5 = routine, 6-8 = genuinely moves "
            "the thesis (earnings surprises, guidance, M&A, FDA, major contracts, insider "
            "clusters), 9-10 = drop-everything. Judge ONLY from the text given. Return JSON "
            '{"scores":{"<id>":{"s":<int>,"why":"<max 12 words>"}}}\n\n' + listing,
            num_predict=1200)
        got = v.get("scores", {}) if isinstance(v, dict) else {}
        for i in batch:
            e = got.get(i["id"], {})
            scores[i["id"]] = {"ticker": i["ticker"], "headline": i["headline"],
                               "datetime": i["datetime"], "url": i["url"],
                               "score": int(e.get("s", -1)) if str(e.get("s", "")).lstrip("-").isdigit() else -1,
                               "why": str(e.get("why", ""))[:90]}
    # keep the file bounded: drop entries older than 14 days
    old = time.time() - 14 * 86400
    prev_alerted = set()
    try:
        prev_alerted = set(json.load(open(OUT)).get("alerted", []))
    except Exception:
        pass
    # drop entries older than 14 days AND entries for tickers no longer in the
    # universe (book independence 2026-08-12 left 38 stale scores from the other book polluting
    # the analyst-desk view — the scorer's memory must track the universe)
    uni = set(feed.get("universe") or [])
    scores = {k: v for k, v in scores.items()
              if (v.get("datetime") or time.time()) > old and v.get("ticker") in uni}

    # (1) HOT-NEWS ESCALATION: score >= 8 among newly scored items -> one ntfy push
    hot_new = [(i["id"], scores[i["id"]]) for i in todo
               if scores.get(i["id"], {}).get("score", -1) >= 8 and i["id"] not in prev_alerted]
    if hot_new:
        msg = "; ".join(f"{v['ticker']} {v['score']}/10 — {v['headline'][:60]} ({v['why']})"
                        for _, v in hot_new[:4])
        subprocess.run([os.path.expanduser("~/maintenance/bin/notify.sh"), "stocks",
                        "Hot news (local AI)", msg], timeout=30)
        prev_alerted.update(k for k, _ in hot_new)

    json.dump({"scored_at": int(time.time()),
               "scored_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "model": f"{DEFAULT_MODEL} (local)",
               "alerted": sorted(prev_alerted)[-200:], "scores": scores},
              open(OUT, "w"), indent=1)

    # (2) NEWS BRIEF for the Claude weekly digest: compact top-scored file so the
    # expensive session reads ~1k tokens of signal instead of the raw feed
    fresh30 = time.time() - 30 * 3600
    ranked = sorted((v for v in scores.values()
                     if (v.get("datetime") or 0) > fresh30 and v["score"] >= 4),
                    key=lambda x: -x["score"])[:12]
    with open(os.path.join(BASE, "data", "news_brief.md"), "w") as f:
        f.write(f"# News brief — scored by local model, {time.strftime('%F %H:%M')} UTC\n"
                "_Top materiality-scored items, last ~30h. Score 0-10; noise (<4) excluded._\n\n")
        for v in ranked:
            f.write(f"- **{v['ticker']}** [{v['score']}/10] {v['headline']} — {v['why']}\n")

    hot = sorted((v for v in scores.values() if v["score"] >= 7),
                 key=lambda x: -x["score"])[:5]
    print(f"{time.strftime('%F %T')} scored {len(todo)} new / {len(scores)} total; "
          f"alerted {len(hot_new)} hot; brief has {len(ranked)} items; "
          f"top: {['%s %s' % (h['ticker'], h['score']) for h in hot]}")


if __name__ == "__main__":
    main()
