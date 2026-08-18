#!/usr/bin/env python3
"""THE ORG CHART — who works on this book, what they read, what they write.

Why this file exists (David, 2026-08-18): "the PM should have a very clear understanding
of the architecture we have and this should be continuously updated as we make changes...
it should know what the COO/VP/fixers/coded stuff etc. are doing every time it logs in...
It is the PM, it should act like a PM and manage its own employees."

The org chart is DATA here, not prose in a prompt, for one reason: prose goes stale and
nobody notices. Every row below carries the files it reads and writes, so `brief()` can
stat them and report what ACTUALLY ran and how fresh its output is. A role whose output
is missing or stale shows up as such on the PM's desk the next morning without anyone
remembering to update a document.

Consumers:
  * loop.py       — desk item 0: the PM reads this before anything else
  * agent_page.py — the "Who runs this book" card on the dashboard
  * ops.py        — the fixer and COO get their own charter from here

CLI:
  roster.py brief          write data/roster_brief.md (the PM's org chart, with live state)
  roster.py json           machine-readable roster + live state
  roster.py charter <role> the allowed-edit surface for a role, one path per line
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
ROOT = ENGINE.parent
DATA = HERE / "data"
LOGS = ENGINE / "logs"
JOURNAL = HERE / "journal"
BRIEF = DATA / "roster_brief.md"

# ---------------------------------------------------------------- the org chart
# reads/writes are paths RELATIVE to the repo root, so they render as links and stat cleanly.
# `charter` is the allowed-edit surface — empty list means "writes no code".
ROLES = [
    {
        "id": "pm", "name": "PM — portfolio manager", "who": "Opus · broker access",
        "model": "opus",
        "cadence": "14:05 UTC market days (+ event-triggered, ACTION cap 3/day)",
        "purpose": "The only role that moves dollars. Judgment: question, re-evaluate, decide, "
                   "and write a pre-trade memo before every order.",
        "reads": ["_engine/agent/data/roster_brief.md", "_engine/agent/data/vp_brief.md",
                  "_engine/agent/data/session_brief.md", "_engine/agent/data/bench_brief.md",
                  "_engine/agent/data/unknowns.md", "_engine/agent/data/patch_requests.json",
                  "_engine/agent/journal/DIRECTIVES.md", "_engine/agent/journal/BOOK.md",
                  "_engine/agent/data/candidates.json", "_engine/agent/data/feed.json"],
        "writes": ["_engine/agent/journal/BOOK.md", "_engine/agent/journal/decisions.md",
                   "_engine/agent/data/thesis.json", "_engine/agent/universe.txt"],
        "charter": ["everything except: it may not silently overrule DIRECTIVES.md"],
        "manages": ["vp", "fixer", "coo", "bench", "cannibal", "numwatch", "scout"],
    },
    {
        "id": "vp", "name": "VP — night sweep and prep desk",
        "who": "code + local models, then a Sonnet review pass",
        # David 2026-08-18: "promote the VP to the best claude sonnet model for now. give it a
        # little boost in intelligence until things run smoothly." The 16 coded stages are
        # unchanged and still cost zero tokens; what Sonnet adds is a REVIEW of their output
        # before the PM sees it — the judgment a stage table cannot make.
        "model": "sonnet",
        "cadence": "20:40 UTC market days",
        "purpose": "Prepares the PM's desk before it wakes: rebuilds cards, runs the numbers "
                   "watchdog, refreshes intel, scores and triages the funnel, rebuilds a dossier "
                   "per held name, runs a bench pass. PREPARATION, never a filter — prepared is "
                   "not cleared, and a FAILED stage is the PM's to do by hand.",
        "reads": ["_engine/agent/universe.txt", "_engine/agent/data/portfolio.json"],
        "writes": ["_engine/agent/data/vp_brief.md", "_engine/agent/data/session_brief.md"],
        "charter": [],
        "runs": ["fincard", "numwatch", "quality", "feeds", "relevance", "scout", "dossier", "bench"],
    },
    {
        "id": "bench", "name": "The Bench — the long local read",
        "who": "local model (role `bulk`) · zero Claude tokens",
        "cadence": "22:00 UTC EVERY night including weekends, 5h window",
        "purpose": "Reads primary filings across the whole market for the narrative-vs-contract "
                   "gap — not a screen. Durable queue, stateless workers; ranked by EVIDENCE "
                   "FOUND (verbatim quotes), never by a multiple.",
        "reads": ["_engine/agent/data/bench_queue.json", "_engine/agent/data/bench_corpus"],
        "writes": ["_engine/agent/data/bench.json", "_engine/agent/data/bench_brief.md"],
        "charter": [],
    },
    {
        "id": "cannibal", "name": "cannibal — whole-market cannibal screen",
        "who": "pure code + one quote pass, watched by a local model · zero Claude tokens",
        "cadence": "22:30 UTC DAILY (was Sundays; David 2026-08-18: \"why can\'t cannibal run daily?\")",
        "purpose": "XBRL frames over every filer: positive FCF, net cash, shrinking share count. "
                   "Feeds the scout funnel as leads, never as theses.",
        "reads": ["SEC XBRL frames API"],
        "writes": ["_engine/agent/data/cannibal.json"],
        "charter": [],
    },
    {
        "id": "scout", "name": "scout — origination funnel",
        "who": "code + local model · zero Claude tokens",
        "cadence": "every 30 min, market hours (inside the feeds chain) + in the VP sweep",
        "purpose": "Collects events (13D subjects, spins, 13F stakes, insider clusters, cannibal "
                   "hits), pre-triages each with a mechanism score and fincard numbers attached. "
                   "A triage score is a LEAD, never a thesis.",
        "reads": ["_engine/agent/data/feed.json", "_engine/agent/data/cannibal.json"],
        "writes": ["_engine/agent/data/candidates.json"],
        "charter": [],
    },
    {
        "id": "numwatch", "name": "numwatch — numbers watchdog",
        "who": "local model extracts · CODE compares · zero Claude tokens",
        "cadence": "nightly inside the VP sweep",
        "purpose": "Audits the PM's OWN PROSE against the fincards and filings. It exists because "
                   "every real error so far lived in prose no code path touched.",
        "reads": ["_engine/agent/journal/*.md", "_engine/agent/names/*/fincard.json"],
        "writes": ["_engine/agent/data/numwatch.json", "_engine/agent/names/*/numcheck.json"],
        "charter": [],
    },
    {
        "id": "fixer", "name": "Fixer — overnight data quality",
        "who": "Sonnet · NO broker access",
        # Was Opus. David 2026-08-18: "Fixer should go down to sonnet model rather than opus."
        # This is the per-job sign-off PROJECT_STANDARDS §2 requires for a model downgrade.
        # Defensible: the fixer's work is mechanical root-cause repair against a queue, every
        # change proven by a re-scan, and its judgment calls are explicitly deferred to the PM.
        "model": "sonnet",
        "cadence": "07:05 UTC Tue–Sat",
        "purpose": "Works the quality queue to zero open. Fixes the number/evidence pipeline at "
                   "the root — tag maps, extraction prompts, watchdog logic — and PROVES each fix "
                   "survives a re-scan. Never adjudicates a thesis question.",
        "reads": ["_engine/agent/data/quality_queue.json", "_engine/agent/names"],
        "writes": ["_engine/agent/journal/ops/<date>_fixer.md",
                   "_engine/agent/data/patch_requests.json"],
        # WIDENED 2026-08-18 — see CHARTER_NOTE below.
        "charter": ["_engine/valuation/fincard.py", "_engine/valuation/query.py",
                    "_engine/agent/dossier.py", "_engine/agent/numwatch.py",
                    "_engine/agent/quality.py", "_engine/agent/refresh_cards.py",
                    "_engine/agent/scout.py", "_engine/agent/cannibal.py"],
        "escalates_to": "pm",
    },
    {
        "id": "coo", "name": "COO — weekend process review",
        "who": "Opus · NO broker access",
        # Stays Opus: the COO's job is adversarial re-derivation from primary sources — the
        # one role whose whole value is catching what the cheaper roles got wrong.
        "model": "opus",
        "cadence": "15:00 UTC Saturdays",
        "purpose": "Adversarial toward our own pipeline. Every known problem must have an owner "
                   "and a plan; re-derives two held positions' numbers from primary sources by "
                   "hand; checks that 'fixed' claims actually hold.",
        "reads": ["_engine/agent/data/quality_queue.json", "_engine/agent/journal",
                  "_engine/logs"],
        "writes": ["_engine/agent/journal/ops/<date>_coo.md"],
        "charter": ["same surface as the fixer — prefers assigning over hot-fixing on a weekend"],
        "escalates_to": "pm",
    },
    {
        "id": "triggers", "name": "triggers — coded escalation",
        "who": "pure code · zero Claude tokens unless a rule trips",
        "cadence": "every 5 min, US market hours",
        "purpose": "Watches price/news rules against thesis.json and wakes the PM only when a "
                   "stated tripwire actually trips.",
        "reads": ["_engine/agent/data/thesis.json", "_engine/agent/data/feed.json"],
        "writes": ["_engine/logs/triggers.log"],
        "charter": [],
    },
    {
        "id": "feeds", "name": "feeds + relevance — intel collection and scoring",
        "who": "code fetches · local model scores · zero Claude tokens",
        "cadence": "every 30 min, pre-market → close, market days",
        "purpose": "News, filings, earnings, the situations radar (13Ds/spins/delistings) and 13F "
                   "manager flow, with a 0-10 materiality score and one-line reason per item.",
        "reads": ["_engine/agent/universe.txt"],
        "writes": ["_engine/agent/data/feed.json", "_engine/agent/data/feed_scored.json"],
        "charter": [],
    },
    {
        "id": "fincard", "name": "fincard — the number pipeline",
        "who": "pure code, SEC XBRL + live quote",
        "cadence": "nightly inside the VP sweep",
        "purpose": "Every figure the book quotes, computed from the issuer's own XBRL with its "
                   "formula and source tag attached. A figure it cannot source is FLAGGED, never "
                   "guessed — see the numbers rule in MANDATE.md.",
        "reads": ["SEC companyfacts", "_engine/agent/names/*/filings"],
        "writes": ["_engine/agent/names/*/fincard.json", "_engine/agent/data/unknowns.md"],
        "charter": [],
    },
    {
        "id": "contract", "name": "contract — the book's own invariants",
        "who": "pure code",
        "cadence": "every PM session + the COO review",
        "purpose": "13 checks the book must always satisfy (re-underwrite age, memo existence, "
                   "journal size, anchor provenance, book arithmetic). A violation is a defect.",
        "reads": ["_engine/agent/journal", "_engine/agent/data"],
        "writes": ["stdout — violations"],
        "charter": [],
    },
]

CHARTER_NOTE = """\
**Charter change 2026-08-18 (David: "i don't understand the fixer charter and what's blocking.
do what makes sense... the PM should manage its own employees").** The fixer's allowed-edit
surface used to be four files, and `numwatch.py` was not among them — so the fixer could
diagnose its own instrument misfiring and was forbidden to fix it. On 2026-08-18 that had
grown to ~100 queue items behind six known one-file edits. The surface now covers the whole
number/evidence pipeline (the files listed above), because none of them decides a trade.

What stayed hard-locked, and always will: `loop.py`, `MANDATE.md`, `triggers.py`, `thesis.json`,
journal memos — anything that decides or records a trade.

Anything the fixer wants changed OUTSIDE its surface is now a **patch request**: it writes the
diagnosis, the verification, and the exact change to `data/patch_requests.json`, and that lands
on the PM's desk. The PM approves, rejects, or escalates to David. Nothing is stranded any more;
it is either fixed, or it is a decision with a named owner sitting in front of the PM.
"""


def _rel(p):
    return str(Path(p)) if not str(p).startswith("/") else str(Path(p).relative_to(ROOT))


def _stat(rel):
    """Freshness of a written artifact. Globs and non-paths resolve to unknown, not to a guess."""
    if "*" in rel or "<" in rel or not rel.startswith("_engine"):
        return None
    p = ROOT / rel
    if not p.exists():
        return {"exists": False}
    m = p.stat().st_mtime
    return {"exists": True, "mtime": m,
            "age_min": int((dt.datetime.now().timestamp() - m) / 60),
            "size": p.stat().st_size}


def _age_str(age_min):
    if age_min is None:
        return "—"
    if age_min < 90:
        return f"{age_min}m ago"
    if age_min < 60 * 48:
        return f"{age_min // 60}h ago"
    return f"{age_min // 1440}d ago"


def live():
    """The roster with each role's output freshness attached."""
    out = []
    for r in ROLES:
        outputs = []
        for w in r["writes"]:
            st = _stat(w)
            outputs.append({"path": w, "state": st})
        newest = [o["state"]["age_min"] for o in outputs
                  if o["state"] and o["state"].get("exists")]
        out.append({**r, "outputs": outputs,
                    "last_output_age_min": min(newest) if newest else None})
    return out


def claude_model(role_id, default="opus"):
    """Which Claude model a role thinks on. Declared here so the org chart is the single
    place a role's cost/capability is set — ops.py and vp.py read it rather than each
    carrying its own --model flag, which is how the fixer stayed on Opus for weeks after
    its work had become mechanical."""
    for r in ROLES:
        if r["id"] == role_id:
            return r.get("model") or default
    return default


def charter(role_id):
    for r in ROLES:
        if r["id"] == role_id:
            return r.get("charter", [])
    return []


def brief():
    """The PM's org chart, regenerated from live state every night. Never hand-edited."""
    rs = live()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import asof
        _hdr = asof.header("Org chart")
    except Exception:
        _hdr = ""
    L = [f"# Your staff — the org chart, as of {now}", "", _hdr,
         "_Generated by `roster.py brief` from the live file system, not hand-written: if a role's",
         "output is missing or stale it says so here rather than going quietly out of date. You are",
         "the PM. These are your employees. Read what they produced, tell them when they are wrong,",
         "and change their instructions when the instructions are the problem._", ""]

    L += ["## Who ran, and how fresh their work is", "",
          "| role | who | model | cadence | latest output |", "|---|---|---|---|---|"]
    for r in rs:
        age = _age_str(r["last_output_age_min"])
        missing = [o["path"] for o in r["outputs"]
                   if o["state"] is not None and not o["state"].get("exists")]
        note = f" · ⚠ missing: {', '.join(Path(m).name for m in missing)}" if missing else ""
        L.append(f"| **{r['name']}** | {r['who']} | `{r.get('model') or '—'}` | "
                 f"{r['cadence']} | {age}{note} |")
    L += [""]

    L += ["## What each one is for, and what it hands you", ""]
    for r in rs:
        L.append(f"### {r['name']}")
        L.append(f"_{r['who']} · {r['cadence']}_")
        L.append("")
        L.append(r["purpose"])
        L.append("")
        writes = ", ".join(f"`{w}`" for w in r["writes"])
        L.append(f"- **Hands you:** {writes}")
        if r.get("runs"):
            L.append(f"- **Runs:** {', '.join(r['runs'])}")
        if r.get("charter"):
            L.append(f"- **May edit:** {', '.join(f'`{c}`' for c in r['charter'])}")
        if r.get("escalates_to"):
            L.append(f"- **Escalates to:** {r['escalates_to'].upper()} "
                     f"(via `data/patch_requests.json`) — that is you.")
        L.append("")

    L += ["## Your authority over this staff", "",
          "- **You may change any of their instructions.** The prompts live in `ops.py` (fixer, COO)",
          "  and `vp.py` (the night sweep); the org chart itself is `roster.py`. If a role is",
          "  producing noise, say so in your session log and change it — do not work around it",
          "  session after session.",
          "- **Patch requests are yours to decide.** `data/patch_requests.json` holds changes the",
          "  fixer diagnosed and verified but is not permitted to make. Approve, reject with a",
          "  reason, or escalate to David. An unanswered request is you not doing your job.",
          "- **Adding a role, or retiring one, is a proposal to David** — write it in BOOK.md under",
          "  Research wanted with the mechanism and the cost.", "",
          "## Charter note", "", CHARTER_NOTE]

    BRIEF.parent.mkdir(parents=True, exist_ok=True)
    BRIEF.write_text("\n".join(L))
    return BRIEF


def _cli(argv):
    cmd = argv[0] if argv else "brief"
    if cmd == "brief":
        p = brief()
        print(f"roster brief: {len(ROLES)} roles -> {p}")
        return 0
    if cmd == "json":
        print(json.dumps(live(), indent=1, default=str))
        return 0
    if cmd == "charter":
        if len(argv) < 2:
            print("usage: roster.py charter <role>", file=sys.stderr)
            return 2
        for c in charter(argv[1]):
            print(c)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
