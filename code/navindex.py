#!/usr/bin/env python3
"""
Local-model navigation notes for an evidence pack — ADDITIVE ONLY.

qwen (via ollama, zero Claude tokens) reads each filing text in _evidence/ and
appends analyst-oriented pointers to INDEX.md: what lives in this document that
a five-pillar teardown will want, and roughly where. Doctrine (token-efficiency
skill, David 2026-08-11): local output is a navigation aid — Opus agents always
retain full raw access; nothing here filters or replaces primary text.

CLI: navindex.py <path-to-_evidence-dir>
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
import models  # noqa: E402  — Mission Control's local-model registry
import gpu     # noqa: E402  — and its GPU queue: one card, Stocks first, maintenance last

# The tag comes from the registry, never from here (~/maintenance/config/models.json).
# require() pre-checks: if ollama is down or the role has no installed model, this exits
# 75 now rather than after we've walked the evidence pack.
OLLAMA = models.chat_url()
MODEL = models.require("dense", job="navindex")   # think:false is still REQUIRED — see ask()
CHUNK = 24_000             # chars of each doc the local model sees (head-biased; sections.json covers the rest)

PROMPT = """You are indexing a SEC filing excerpt for an equity analyst who will deep-read it later.
Output EXACTLY 5 bullet lines, each "- <topic>: <where/what — one clause>". Topics to look for
(only if present): revenue drivers/segments, contract or royalty terms, guidance, risk factors that
are specific (not boilerplate), capital structure/dilution, litigation/IP, insider or governance
items. Be concrete (name numbers, parties, section names). No preamble, no closing line."""


def ask(text):
    body = json.dumps({"model": MODEL, "think": False, "stream": False,
                       "messages": [{"role": "system", "content": PROMPT},
                                    {"role": "user", "content": text}],
                       "options": {"num_predict": 300, "temperature": 0.2}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with gpu.slot(job="navindex", model=MODEL):
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["message"]["content"].strip()


def annotate(ev_dir):
    ev = Path(ev_dir)
    idx = ev / "INDEX.md"
    docs = sorted((ev / "filings").glob("*.txt"))
    if not docs or not idx.exists():
        print("navindex: nothing to annotate")
        return
    notes = ["", "## Local-model reading notes (qwen — pointers only, verify in the raw text)"]
    for d in docs:
        try:
            out = ask(f"FILING FILE: {d.name}\n\n{d.read_text(errors='replace')[:CHUNK]}")
            notes.append(f"\n### {d.name}\n{out}")
            print(f"navindex: {d.name} ok")
        except Exception as e:
            print(f"navindex: {d.name} failed ({str(e)[:60]})")
    idx.write_text(idx.read_text() + "\n".join(notes) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: navindex.py <_evidence dir>")
    annotate(sys.argv[1])
