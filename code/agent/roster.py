#!/usr/bin/env python3
"""THE ORG CHART — who works on this book, what they read, what they write.

Why this file exists (David, 2026-08-18): "the PM should have a very clear understanding
of the architecture we have and this should be continuously updated as we make changes...
it should know what the COO/VP/fixers/coded stuff etc. are doing every time it logs in...
It is the PM, it should act like a PM and manage its own employees."

The org chart is DATA here, not prose in a prompt, for one reason: prose goes stale and
nobody notices. Every row below carries the files it reads and writes, so `brief()` can
stat them and report what ACTUALLY ran and how fresh its output is. Since 2026-09-01 a
role's CADENCE is read from `crontab -l` too (the `cron` matcher names its line), never
typed here: the VP moved from 20:40Z to 04:05Z on 2026-08-28 and this chart said 20:40Z
for four days while calling itself authoritative. A role whose matcher finds no crontab
line renders as NOT IN CRONTAB — the `ops.py hunt` failure class, visible on the desk. A role whose output
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
  roster.py cron           every role's cadence as the crontab actually has it
  roster.py relays         producer -> artifact -> readers, derived; exit 1 on an unread output
  roster.py schedule       every crontab line in ET with its five-hour usage window and Claude tokens
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _stamp():
    """Provenance stamp so this file's CONTENT drift is measurable later, not just its age."""
    try:
        import asof
        return asof.stamp()
    except Exception:
        return ""



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
        "id": "pm", "name": "PM — portfolio manager", "who": "best Claude available · broker access",
        # David 2026-09-01: "I want the PMs to run on fable 5.1 going forward / always the best
        # model available." `best` is a TIER (CLAUDE_TIERS below): first entry the CLI can run wins.
        "model": "best",
        "cron": "loop.py trade", "cadence_note": "+ event-triggered (triggers.py ACTION, cap 3/day)",
        "purpose": "The only role that moves dollars. Judgment: question, re-evaluate, decide, "
                   "and write a pre-trade memo before every order.",
        "reads": ["_engine/agent/data/roster_brief.md", "_engine/agent/data/vp_brief.md",
                  "_engine/agent/data/session_brief.md", "_engine/agent/data/bench_brief.md",
                  "_engine/agent/data/unknowns.md", "_engine/agent/data/patch_requests.json",
                  "_engine/agent/data/reflections.json", "_engine/agent/data/card_changes.md",
                  "_engine/agent/data/asks.json", "_engine/agent/journal/ops/<date>_*.md",
                  "_engine/agent/journal/DIRECTIVES.md", "_engine/agent/journal/BOOK.md",
                  "_engine/agent/data/candidates.json", "_engine/agent/data/feed.json",
                  "_engine/agent/data/feed_scored.json", "_engine/agent/names/*/fincard.json",
                  "_engine/agent/names/*/numcheck.json", "_engine/agent/data/portfolio.json",
                  "_engine/agent/data/trades.json", "_engine/agent/data/thesis.json",
                  "_engine/agent/data/learning_brief.md", "_engine/agent/data/kpis.json",
                  "_engine/agent/data/desk.md", "_engine/agent/data/analyst_brief.md"],
        "writes": ["_engine/agent/journal/BOOK.md", "_engine/agent/journal/decisions.md",
                   "_engine/agent/journal/sessions/<date>_session.md", "_engine/agent/journal/<date>_<memo>.md",
                   "_engine/agent/data/thesis.json", "_engine/agent/universe.txt",
                   "_engine/agent/data/asks.json", "_engine/agent/data/patch_requests.json",
                   "_engine/agent/data/candidates.json", "_engine/agent/data/dates.json",
                   "_engine/agent/data/trades.json", "_engine/agent/prompts/<role>.md",
                   "_engine/agent/data/predictions.json", "_engine/agent/data/kpis.json",
                   "_engine/agent/journal/LESSONS.md", "_engine/agent/journal/STRATEGY.md"],
        "charter": ["everything except: it may not silently overrule DIRECTIVES.md"],
        "manages": ["vp", "numbers", "signals", "coo", "hunt", "bench", "cannibal",
                    "numwatch", "scout"],
    },
    {
        "id": "vp", "name": "VP — night sweep and prep desk",
        "who": "code + local models, then a Sonnet review pass",
        # David 2026-08-18: "promote the VP to the best claude sonnet model for now. give it a
        # little boost in intelligence until things run smoothly." The 16 coded stages are
        # unchanged and still cost zero tokens; what Sonnet adds is a REVIEW of their output
        # before the PM sees it — the judgment a stage table cannot make.
        "model": "sonnet",
        "cron": "vp.py sweep",
        "purpose": "Prepares the PM's desk before it wakes: rebuilds cards, runs the numbers "
                   "watchdog, refreshes intel, scores and triages the funnel, rebuilds a dossier "
                   "per held name, runs a bench pass. PREPARATION, never a filter — prepared is "
                   "not cleared, and a FAILED stage is the PM's to do by hand.",
        "reads": ["_engine/agent/prompts/vp.md", "_engine/agent/universe.txt", "_engine/agent/data/portfolio.json",
                  "_engine/agent/data/feed.json", "_engine/agent/data/candidates.json",
                  "_engine/agent/data/bench_brief.md", "_engine/agent/names/*/fincard.json",
                  "_engine/agent/data/unknowns.md", "_engine/agent/data/quality_queue.json",
                  "_engine/agent/data/kpis.json"],
        "writes": ["_engine/agent/data/vp_brief.md", "_engine/agent/data/session_brief.md",
                   "_engine/agent/data/roster_brief.md", "_engine/agent/data/unknowns.md",
                   "_engine/agent/data/card_changes.md", "_engine/agent/data/numwatch.json",
                   "_engine/agent/names/*/numcheck.json", "_engine/agent/names/*/manifest.json",
                   "_engine/agent/data/quality_queue.json", "_engine/agent/data/feed_scored.json"],
        "charter": [],
        "runs": ["fincard", "numwatch", "quality", "feeds", "relevance", "scout", "dossier", "bench",
                 "analyst", "desk"],
    },
    {
        "id": "analyst", "name": "Analyst — the overnight documents-first re-underwrite",
        "who": "best Opus · NO broker access · inside the VP sweep",
        # David 2026-09-01: move the analyst work off the PM — the human split is analyst
        # re-derives, PM decides. Opus, not Sonnet (same evening): the brief is evidence the PM
        # acts on after verifying a sample, so a subtly wrong number costs more than the tokens;
        # the COO stays on Opus for the same reason. Runs ~00:05 ET, clear of the trade lookback.
        "model": "opus",
        "cron": "vp.py sweep", "cadence_note": "the rotation name (oldest last_reunderwrite) each night",
        "purpose": "Re-derives one held position from the filings and the fincard FIRST and the "
                   "PM's memos last: the numbers quoted, the divergences from the PM's prose, the "
                   "anchor the documents support, the predictions and KPIs it would file, what only "
                   "a judgment call resolves. The PM verifies a sample and decides.",
        "reads": ["_engine/agent/prompts/analyst.md", "_engine/agent/names/*/fincard.json",
                  "_engine/agent/names/*/terms.json", "_engine/agent/names/*/filings",
                  "_engine/agent/data/feed.json", "_engine/agent/data/thesis.json",
                  "_engine/agent/journal/<date>_<memo>.md", "_engine/agent/journal/BOOK.md"],
        "writes": ["_engine/agent/data/analyst_brief.md"],
        "charter": [],
    },
    {
        "id": "desk", "name": "desk — the packet",
        "who": "pure code",
        "cron": "vp.py sweep", "cadence_note": "end of the VP sweep + rebuilt by loop.py seconds before every PM launch",
        "purpose": "One file the PM reads first: the top of every brief with the path to the rest, "
                   "the questions this session must answer (from what changed), the inbox, the book "
                   "table with book-level facts, the analyst's brief, unknowns, staff verdict lines, "
                   "learning breaches, the bench top, org-chart health.",
        "reads": ["_engine/agent/data/session_brief.md", "_engine/agent/data/vp_brief.md",
                  "_engine/agent/data/analyst_brief.md", "_engine/agent/data/unknowns.json",
                  "_engine/agent/data/card_changes.md", "_engine/agent/journal/ops/<date>_*.md",
                  "_engine/agent/data/learning_brief.md", "_engine/agent/data/bench_brief.md",
                  "_engine/agent/data/feed_scored.json", "_engine/agent/data/alerts.json",
                  "_engine/agent/data/asks.json", "_engine/agent/data/portfolio.json",
                  "_engine/agent/data/thesis.json", "_engine/agent/journal/DIRECTIVES.md",
                  "_engine/agent/data/patch_requests.json", "_engine/agent/data/reflections.json",
                  "_engine/agent/journal/sessions/*_session.md"],
        "writes": ["_engine/agent/data/desk.md"],
        "charter": [],
    },
    {
        "id": "bench", "name": "The Bench — the long local read",
        "who": "local model (role `bulk`) · zero Claude tokens",
        "cron": "bench.py work", "cadence_note": "5h window, every night incl. weekends",
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
        "cron": "cannibal.py run", "cadence_note": "daily since 2026-08-18 (David: \"why can't cannibal run daily?\")",
        "purpose": "XBRL frames over every filer: positive FCF, net cash, shrinking share count. "
                   "Feeds the scout funnel as leads, never as theses.",
        "reads": ["SEC XBRL frames API"],
        "writes": ["_engine/agent/data/cannibal.json"],
        "charter": [],
    },
    {
        "id": "scout", "name": "scout — origination funnel",
        "who": "code + local model · zero Claude tokens",
        "cron": ["scout.py run", "vp.py sweep"], "cadence_note": "inside the feeds chain + the VP sweep",
        "purpose": "Collects events (13D subjects, spins, 13F stakes, insider clusters, cannibal "
                   "hits), pre-triages each with a mechanism score and fincard numbers attached. "
                   "A triage score is a LEAD, never a thesis.",
        "reads": ["_engine/agent/data/feed.json", "_engine/agent/data/feed_scored.json",
                  "_engine/agent/data/cannibal.json", "_engine/agent/names/*/fincard.json"],
        "writes": ["_engine/agent/data/candidates.json"],
        "charter": [],
    },
    {
        "id": "numwatch", "name": "numwatch — numbers watchdog",
        "who": "local model extracts · CODE compares · zero Claude tokens",
        "cron": "vp.py sweep", "cadence_note": "inside the VP sweep",
        "purpose": "Audits the PM's OWN PROSE against the fincards and filings. It exists because "
                   "every real error so far lived in prose no code path touched.",
        "reads": ["_engine/agent/journal/*.md", "_engine/agent/names/*/fincard.json"],
        "writes": ["_engine/agent/data/numwatch.json", "_engine/agent/names/*/numcheck.json"],
        "charter": [],
    },
    {
        "id": "numbers", "name": "Numbers Engineer — accounting forensics (was: Fixer)",
        "who": "Sonnet · NO broker access",
        # Was Opus. David 2026-08-18: "Fixer should go down to sonnet model rather than opus."
        # This is the per-job sign-off PROJECT_STANDARDS §2 requires for a model downgrade.
        # Defensible: the work is mechanical root-cause repair against a queue, every change
        # proven by a re-scan, and its judgment calls are explicitly deferred to the PM.
        # RENAMED fixer -> numbers 2026-08-19 (ORG_PLAN): the org now has two engineers split
        # by DOMAIN, and "fixer" said nothing about which domain. `fixer` stays a working
        # alias everywhere (ops.py CLI, asks.py addresses) so nothing breaks mid-transition.
        "model": "sonnet",
        "cron": ["ops.py numbers", "ops.py fixer"],   # crontab still says fixer (alias)
        "purpose": "Owns the NUMBER pipeline (owners.py: subsystem `numbers`) — XBRL to card "
                   "to dossier to the watchdog. Works the quality queue and its ask inbox to "
                   "zero, fixes at the root, and PROVES each fix survives a re-scan. Never "
                   "adjudicates a thesis question.",
        "reads": ["_engine/agent/prompts/numbers.md", "_engine/agent/data/quality_queue.json", "_engine/agent/names",
                  "_engine/agent/data/asks.json"],
        "writes": ["_engine/agent/journal/ops/<date>_numbers.md",
                   "_engine/agent/data/patch_requests.json", "_engine/agent/data/asks.json",
                   "_engine/agent/names/*/fincard.json", "_engine/agent/data/quality_queue.json"],
        # charter comes from owners.py (single source of truth) — see charter() below.
        "charter": [],
        "escalates_to": "pm",
    },
    {
        "id": "signals", "name": "Signals Engineer — signal-vs-noise",
        "who": "Sonnet · NO broker access",
        # NEW 2026-08-19 (ORG_PLAN, approved by David): bench/feeds/triggers/scout/cannibal —
        # ~2,000 lines of origination and intel code — had no owner at all. Sonnet for the
        # same reason the Numbers Engineer is: mechanical repair against evidence, judgment
        # deferred to the PM. Scheduled 30 min after Numbers so no new usage window opens
        # and the two never edit concurrently.
        "model": "sonnet",
        "cron": "ops.py signals",
        "purpose": "Owns the SIGNAL pipeline (owners.py: subsystem `signals`) — scout, feeds, "
                   "relevance, cannibal, the Bench, insider clusters, trigger RULES. Its "
                   "question every session: is each feed actually saying something, or alive "
                   "and mute? A silent pipeline is its defect even when no row says so.",
        "reads": ["_engine/agent/prompts/signals.md", "_engine/agent/data/feed.json", "_engine/agent/data/candidates.json",
                  "_engine/agent/data/bench_brief.md", "_engine/agent/data/cannibal.json",
                  "_engine/agent/data/asks.json"],
        "writes": ["_engine/agent/journal/ops/<date>_signals.md",
                   "_engine/agent/data/patch_requests.json", "_engine/agent/data/asks.json"],
        "charter": [],
        "escalates_to": "pm",
    },
    {
        "id": "build", "name": "Build Engineer — surfaces",
        "who": "Sonnet · NO broker access",
        # NEW 2026-08-28 (owners.py-067, authorized by David "fix everything, no decisions
        # needed me" 2026-08-27): subsystem `surfaces` (the dashboard) had an owner that
        # never ran — 4 asks deep, oldest stalled 5 days, while the /agent page showed a
        # false MISSED daily. Weekly on Friday: surface defects are real but rarely urgent,
        # and the hunt's Thursday run feeds it fresh repros.
        "model": "sonnet",
        "cron": "ops.py build",
        "purpose": "Owns the SURFACES (owners.py: subsystem `surfaces`) — the dashboard and "
                   "every page on it. Works its ask queue: what a card claims must match "
                   "what the data says, on the phone first (mobile-web rules).",
        "reads": ["_engine/agent/prompts/build.md", "_engine/agent/data/asks.json"],
        "writes": ["_engine/agent/journal/ops/<date>_build.md", "_engine/agent/data/asks.json"],
        "charter": [],
        "escalates_to": "pm",
    },
    {
        "id": "coo", "name": "COO — weekend process review",
        "who": "Opus · NO broker access",
        # Stays Opus: the COO's job is adversarial re-derivation from primary sources — the
        # one role whose whole value is catching what the cheaper roles got wrong.
        "model": "opus",
        "cron": "ops.py coo",
        "purpose": "Adversarial toward our own pipeline. Every known problem must have an owner "
                   "and a plan; re-derives two held positions' numbers from primary sources by "
                   "hand; checks that 'fixed' claims actually hold.",
        "reads": ["_engine/agent/prompts/coo.md", "_engine/agent/data/quality_queue.json", "_engine/agent/journal",
                  "_engine/logs", "_engine/agent/data/asks.json", "_engine/agent/data/reflections.json",
                  "_engine/agent/data/roster_brief.md", "_engine/agent/data/unknowns.md",
                  "_engine/agent/journal/LESSONS.md", "_engine/agent/data/learning_brief.md",
                  "_engine/agent/journal/STRATEGY.md"],
        "writes": ["_engine/agent/journal/ops/<date>_coo.md", "_engine/agent/data/asks.json",
                   "_engine/agent/data/reflections.json", "_engine/agent/data/dates.json"],
        # charter from owners.py: the integrity layer + the desk code (contract, sweepcheck,
        # asof, unknowns, roster, asks, owners, COMMS.md, vp.py, ops.py) — prefers assigning
        # to an engineer over hot-fixing on a weekend.
        "charter": [],
        "escalates_to": "david",
    },
    {
        "id": "hunt", "name": "Bug hunter — adversarial defect search",
        "who": "Opus · NO broker access",
        "cron": "ops.py hunt", "cadence_note": "declared TEMPORARY 2026-08-18 (David: \"while we are still early\")",
        "purpose": "Finds defects nobody knows about yet — the opposite job to the COO, which "
                   "asks whether KNOWN problems have owners. OWNS NOTHING, DELIBERATELY: an "
                   "owner auditing its own area is the self-verification problem, and the hunt "
                   "is the role that reads without a boundary — every two-strikes ask (declined "
                   "or deferred twice by its owner) auto-routes here. Rotates through six areas "
                   "on a coverage ledger (the sixth, `seams`, is what falls BETWEEN owners), "
                   "and every finding must carry a reproduction: exact command, observed "
                   "output, expected output, responsible line.",
        "model": "opus",
        "reads": ["_engine/agent/prompts/hunt.md", "_engine/agent/journal/ops/hunt_coverage.json", "_engine/agent/data/asks.json",
                  "the code in its target area"],
        "writes": ["_engine/agent/journal/ops/<date>_hunt.md",
                   "_engine/agent/journal/ops/hunt_coverage.json", "_engine/agent/data/asks.json"],
        "charter": ["may fix within EITHER engineer's surface (owners.py), blast-radius gate "
                    "mandatory if it edits anything"],
        "escalates_to": "pm",
    },
    {
        "id": "triggers", "name": "triggers — coded escalation",
        "who": "pure code · zero Claude tokens unless a rule trips",
        "cron": "triggers.py run",
        "purpose": "Watches price/news rules against thesis.json and wakes the PM only when a "
                   "stated tripwire actually trips.",
        "reads": ["_engine/agent/data/thesis.json", "_engine/agent/data/feed.json",
                  "_engine/agent/data/portfolio.json"],
        "writes": ["_engine/agent/data/alerts.json", "phone (notify.sh)", "PM launch (loop.py trade, ACTION)"],
        "charter": [],
    },
    {
        "id": "feeds", "name": "feeds + relevance — intel collection and scoring",
        "who": "code fetches · local model scores · zero Claude tokens",
        "cron": "feeds.py refresh",
        "purpose": "News, filings, earnings, the situations radar (13Ds/spins/delistings) and 13F "
                   "manager flow, with a 0-10 materiality score and one-line reason per item.",
        "reads": ["_engine/agent/universe.txt"],
        "writes": ["_engine/agent/data/feed.json", "_engine/agent/data/feed_scored.json"],
        "charter": [],
    },
    {
        "id": "fincard", "name": "fincard — the number pipeline",
        "who": "pure code, SEC XBRL + live quote",
        "cron": "vp.py sweep", "cadence_note": "inside the VP sweep",
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
        "cron": None, "cadence_note": "every PM session (loop.py reconcile) + the COO review",
        "purpose": "14 checks the book must always satisfy (re-underwrite age, memo existence, "
                   "journal size, anchor provenance, book arithmetic, open BLOCKING unknowns, "
                   "unanswered patch requests). A violation is a defect. Its sibling "
                   "`sweepcheck.py` guards the number pipeline instead of the book: the "
                   "accounting identity across every card, and a before/after diff that any "
                   "pipeline change must pass before it ships.",
        "reads": ["_engine/agent/journal", "_engine/agent/data"],
        "writes": ["stdout — violations"],
        "charter": [],
    },
    {
        "id": "reflect", "name": "reflect — the reflection ledger",
        "who": "pure code · the PM authors the lessons · the COO verifies",
        "cron": None, "cadence_note": "after every PM session (loop.py reconcile) + rendered into every PM launch",
        "purpose": "How the PM learns from its own mistakes, bounded (REFLECTION.md). Coded "
                   "evaluators turn each session's failures into FINDINGS with a stable signature; "
                   "the PM must answer each with a LESSON + evidence in its `## Reflection`; at most "
                   "three active lessons are injected into the next session; two clean sessions "
                   "absorb one, a repeat is a strike (2 → COO ask, 3 → frozen + David).",
        "reads": ["_engine/agent/journal/sessions/*_session.md", "_engine/agent/data/numwatch.json",
                  "_engine/agent/data/asks.json", "_engine/agent/journal/*.audit.json",
                  "_engine/agent/data/unknowns.json", "_engine/logs/agent_trade.log",
                  "_engine/agent/data/kpis.json"],
        "writes": ["_engine/agent/data/reflections.json", "_engine/agent/data/asks.json"],
        "charter": [],
    },
    {
        "id": "report", "name": "pmreport — David's daily read",
        "who": "pure code",
        "cron": None, "cadence_note": "after every PM session (loop.py reconcile), after the ledger",
        "purpose": "Renders the report David reads from the session log's one-line decisions "
                   "(approve / reject / hold, each with its reason and evidence), the PM's Summary "
                   "and Watching verbatim, and what the machine says about the machine: contract, "
                   "reflections, asks, org-chart freshness, incidents, and a ten-session trend.",
        "reads": ["_engine/agent/journal/sessions/*_session.md", "_engine/agent/data/asks.json",
                  "_engine/agent/data/reflections.json", "_engine/agent/data/portfolio.json",
                  "_engine/agent/data/pm_usage.json", "_engine/agent/data/predictions.json",
                  "_engine/agent/data/kpis.json"],
        "writes": ["_engine/agent/data/pm_report.md"],
        "charter": [],
    },
    {
        "id": "learn", "name": "learn — the prediction loop",
        "who": "pure code · the PM predicts, code resolves, the PM writes the lesson",
        "cron": None, "cadence_note": "after every PM session (loop.py reconcile); the PM answers it on Fridays",
        "purpose": "LEARNING.md: every thesis carries dated, falsifiable predictions with a source code "
                   "can read; code resolves them on the date (hit / miss / needs-key); the KPI watch names "
                   "the numbers each thesis lives on; the Friday brief turns outcomes into ONE change "
                   "in LESSONS.md (C24 / C25).",
        "reads": ["_engine/agent/data/thesis.json", "_engine/agent/journal/<date>_<memo>.md",
                  "_engine/agent/names/*/fincard.json", "_engine/agent/data/feed.json",
                  "_engine/agent/data/portfolio.json", "_engine/agent/data/alerts.json",
                  "_engine/agent/journal/sessions/*_session.md", "_engine/agent/data/pm_usage.json"],
        "writes": ["_engine/agent/data/predictions.json", "_engine/agent/data/kpis.json",
                   "_engine/agent/data/learning_brief.md"],
        "charter": [],
    },
    {
        "id": "usage", "name": "pmusage — where the PM's minutes and tokens went",
        "who": "pure code, from the session transcript",
        "cron": None, "cadence_note": "after every PM session (loop.py reconcile)",
        "purpose": "Minutes and tokens per session by what the PM was doing — reading our reports, new "
                   "research, deciding and writing, managing staff — from the CLI transcript's own "
                   "timestamps and token counts. Nothing estimated. David reads it daily; the PM reads "
                   "the trend on Fridays to see whether its DELEGATE duty is moving hand work to cheaper tiers.",
        "reads": ["_engine/agent/journal/sessions/*_session.md", "~/.claude/projects/<cwd>/<id>.jsonl"],
        "writes": ["_engine/agent/data/pm_usage.json"],
        "charter": [],
    },
]

CHARTER_NOTE = """\
**Charter change 2026-08-18 (David: "i don't understand the fixer charter and what's blocking.
do what makes sense... the PM should manage its own employees").** The fixer's allowed-edit
surface used to be four files, and `numwatch.py` was not among them — so the fixer could
diagnose its own instrument misfiring and was forbidden to fix it. On 2026-08-18 that had
grown to ~100 queue items behind six known one-file edits. The surface now covers the whole
number/evidence pipeline, and since 2026-08-19 every surface is DERIVED from the ownership
map in `owners.py` — one source of truth; a file cannot be owned by two roles or by none.

What stayed hard-locked, and always will: `loop.py`, `MANDATE.md`, `triggers.py`, `thesis.json`,
journal memos — anything that decides or records a trade.

Anything the fixer wants changed OUTSIDE its surface is now a **patch request**: it writes the
diagnosis, the verification, and the exact change to `data/patch_requests.json`, and that lands
on the PM's desk. The PM approves, rejects, or escalates to David. Nothing is stranded any more;
it is either fixed, or it is a decision with a named owner sitting in front of the PM.
"""


# ---------------------------------------------------------------- Claude model tiers
# A role names a TIER, never a bare model id (same doctrine as models.json for local models).
# The launcher tries the list in order and falls back on a "does not support this model" death,
# so a tier can name a model the installed CLI cannot run yet without killing the session.
# Updating the box to a newer model is ONE edit here. `best` is David's standing order for the
# PM ONLY (2026-09-01: "only PM is best (fable 5.1), others can still be opus (best opus), and
# sonnet"): the most capable generally available Claude, currently Fable 5.1. `opus` and
# `sonnet` are the CLI aliases, which track the newest release of each.
CLAUDE_TIERS = {
    "best": ["claude-fable-5-1", "opus"],
    "opus": ["opus"],
    "sonnet": ["sonnet"],
}


def claude_models(role_id, default="opus"):
    """The ordered model list for a role — tier expanded, or the literal id if not a tier."""
    role_id = {"fixer": "numbers"}.get(role_id, role_id)
    tier = default
    for r in ROLES:
        if r["id"] == role_id:
            tier = r.get("model") or default
            break
    return list(CLAUDE_TIERS.get(tier, [tier]))


# ---------------------------------------------------------------- the crontab is the clock
_CRON = {"at": 0.0, "lines": None, "err": None}
_CRON_TTL = 30.0


def crontab_lines(force=False):
    """Every active line of the user's crontab as (minute, hour, dom, mon, dow, command).
    Never raises: on failure returns [] and cron_error() says why. Cached briefly so a
    brief resolving 15 roles shells out once."""
    now = dt.datetime.now().timestamp()
    if not force and _CRON["lines"] is not None and now - _CRON["at"] < _CRON_TTL:
        return _CRON["lines"]
    out, err = [], None
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            err = (r.stderr or "crontab -l failed").strip()[:120]
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ln.startswith("@"):
                tag, _, cmd = ln.partition(" ")
                out.append((tag, "", "", "", "", cmd.strip()))
                continue
            parts = ln.split(None, 5)
            if len(parts) == 6:
                out.append(tuple(parts))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:120]
    _CRON.update(at=now, lines=out, err=err)
    return out


def cron_error():
    crontab_lines()
    return _CRON["err"]


def cron_matches(matcher):
    """Crontab lines whose command mentions the matcher (a string, or any of a list)."""
    if not matcher:
        return []
    keys = [matcher] if isinstance(matcher, str) else list(matcher)
    return [ln for ln in crontab_lines() if any(k in ln[5] for k in keys)]


_DOW = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}


def _dom_text(dom, mon):
    """'on the 1st', 'on the 5th of Mar', '' for every day."""
    if dom in ("*", "?") and mon in ("*", "?"):
        return ""
    def _ord(n):
        return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
    out = ""
    if dom not in ("*", "?"):
        out = "on the " + ", ".join(_ord(int(x)) if x.isdigit() else x for x in dom.split(","))
    if mon not in ("*", "?"):
        out += " of " + mon
    return out.strip()


def _dow_text(field):
    if field in ("*", "?"):
        return "daily"
    parts = []
    for tok in field.split(","):
        if "-" in tok:
            a, b = tok.split("-", 1)
            parts.append(f"{_DOW.get(a, a)}–{_DOW.get(b, b)}")
        else:
            parts.append(_DOW.get(tok, tok))
    return ", ".join(parts)


def _hour_text(minute, hour):
    if minute.startswith("*/"):
        every = f"every {minute[2:]} min"
        if hour == "*":
            return f"{every}, all day"
        if "-" in hour and hour.replace("-", "").isdigit():
            a, b = hour.split("-", 1)
            return f"{every}, {int(a):02d}:00–{int(b):02d}:59"
        return f"{every}, hours {hour}"
    if all(h.isdigit() for h in hour.split(",")) and all(m.isdigit() for m in minute.split(",")):
        return ", ".join(f"{int(h):02d}:{int(m):02d}" for h in hour.split(",") for m in minute.split(","))
    return f"min {minute} hour {hour}"


def _days_text(dom, mon, dow):
    d = _dom_text(dom, mon)
    return d if d else _dow_text(dow)


def cadence_text(line):
    """One crontab line as a human cadence: '14:05 UTC Mon–Fri', 'every 5 min, 13:00–20:59 UTC Mon–Fri',
    '11:00 UTC on the 1st'."""
    minute, hour, dom, mon, dow, _ = line
    if minute.startswith("@"):
        return minute
    return f"{_hour_text(minute, hour)} UTC {_days_text(dom, mon, dow)}"


def cron_cadence(matcher):
    """A role's cadence AS THE CRONTAB HAS IT. 'NOT IN CRONTAB' is a finding, not a blank."""
    if not matcher:
        return ""
    if cron_error():
        return f"crontab unreadable ({cron_error()})"
    seen = []
    for ln in cron_matches(matcher):
        t = cadence_text(ln)
        if t not in seen:
            seen.append(t)
    return "; ".join(seen) if seen else "NOT IN CRONTAB"


def cron_schedule(matcher):
    """Fixed-time launches for a role as [(hour, minute, {python weekdays, Mon=0})] — what a
    finish-check needs. Interval lines (*/N) are skipped: they have no single due time."""
    out = []
    for minute, hour, dom, mon, dow, _ in cron_matches(matcher):
        if not hour.isdigit() or not all(m.isdigit() for m in minute.split(",")):
            continue
        days = set()
        if dow in ("*", "?"):
            days = set(range(7))
        else:
            for tok in dow.split(","):
                if "-" in tok:
                    a, b = tok.split("-", 1)
                    rng = range(int(a), int(b) + 1)
                else:
                    rng = [int(tok)]
                for d in rng:
                    days.add((d - 1) % 7)   # cron Sun=0/7 -> python Sun=6
        for m in minute.split(","):
            out.append((int(hour), int(m), days))
    return out


# ---- ET rendering and the five-hour usage windows (David 2026-09-01: "show all the scheduled
# jobs in EST and note which 5 hour window it's in throughout the day"). Windows are anchored
# on the one that matters: B = 09:05–14:05 UTC, the trade session's five-hour lookback, which
# no Claude cron may start inside (the Monday board is the one exception). The others follow.
WINDOWS = [("A", 5 * 60 + 5, 9 * 60 + 5, "early morning — night jobs' tail"),
           ("B", 9 * 60 + 5, 14 * 60 + 5, "PROTECTED — the 14:05Z trade session's lookback"),
           ("C", 14 * 60 + 5, 19 * 60 + 5, "the trade session + its reconcile"),
           ("D", 19 * 60 + 5, 24 * 60 + 5, "after the close"),
           ("E", 0 * 60 + 5, 5 * 60 + 5, "overnight — VP sweep, analyst, bench")]


def usage_window(hour, minute):
    t = hour * 60 + minute
    for name, a, b, desc in WINDOWS:
        if a <= t < b or (b > 24 * 60 and t < b - 24 * 60 and name == "D"):
            return name
    return "?"


def et_time(hour, minute, day=None):
    """HH:MM UTC -> 'h:mmam ET' for today's date (the ET label moves an hour on DST changes;
    the UTC cron time does not)."""
    try:
        from zoneinfo import ZoneInfo
        d = day or dt.date.today()
        u = dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=dt.timezone.utc)
        e = u.astimezone(ZoneInfo("America/New_York"))
        lab = e.strftime("%I:%M%p").lstrip("0").lower()
        suffix = "" if e.date() == d else (" (prev day)" if e.date() < d else " (next day)")
        return lab + " " + e.strftime("%Z") + suffix
    except Exception:
        return "?"


def cadence_et(line):
    """A crontab line as ET: fixed times become h:mmam EDT; intervals name their ET span."""
    minute, hour, dom, mon, dow, _ = line
    if minute.startswith("@"):
        return minute
    if all(h.isdigit() for h in hour.split(",")) and all(m.isdigit() for m in minute.split(",")):
        return ", ".join(et_time(int(h), int(m)) for h in hour.split(",") for m in minute.split(",")) + " " + _days_text(dom, mon, dow)
    if minute.startswith("*/") and "-" in hour and hour.replace("-", "").isdigit():
        a, b = hour.split("-", 1)
        return f"every {minute[2:]} min, {et_time(int(a), 0)}–{et_time(int(b), 59)} {_days_text(dom, mon, dow)}"
    if minute.startswith("*/"):
        return f"every {minute[2:]} min, all day {_days_text(dom, mon, dow)}"
    return cadence_text(line)


def windows_of(line):
    minute, hour, dom, mon, dow, _ = line
    if minute.startswith("@"):
        return "—"
    if all(h.isdigit() for h in hour.split(",")) and all(m.isdigit() for m in minute.split(",")):
        return "".join(sorted({usage_window(int(h), int(m)) for h in hour.split(",") for m in minute.split(",")}))
    if "-" in hour and hour.replace("-", "").isdigit():
        a, b = (int(x) for x in hour.split("-", 1))
        return "".join(sorted({usage_window(h, 0) for h in range(a, b + 1)}))
    if hour == "*":
        return "ABCDE"
    return "?"


def _claude_weights():
    try:
        w = json.loads((Path.home() / "maintenance/config/job_weights.json").read_text()).get("weights", [])
        return [(x.get("match"), x.get("tokens_per_run", 0)) for x in w]
    except Exception:
        return []


def claude_tokens(cmd):
    """Estimated Claude tokens per run for a crontab command, from Mission Control's ledger
    (0 = code or local models only)."""
    for match, tok in _claude_weights():
        if match and match in cmd:
            return tok
    return 0 if "claude-headless" not in cmd else None


def _sort_key(line):
    minute, hour, *_ = line
    try:
        h = int(hour.split(",")[0].split("-")[0]) if hour not in ("*",) else -1
        m = int(minute.split(",")[0]) if minute.isdigit() or "," in minute else -1
        return (0, h, m) if h >= 0 else (1, 0, 0)
    except Exception:
        return (2, 0, 0)


def schedule():
    """Every crontab line, in UTC order: [(cadence_utc, cadence_et, windows, claude_tokens, command)]."""
    out = []
    for ln in sorted(crontab_lines(), key=_sort_key):
        out.append((cadence_text(ln), cadence_et(ln), windows_of(ln), claude_tokens(ln[5]), ln[5]))
    return out


def cron_drift():
    """Roles that declare a crontab line and do not have one (or the crontab is unreadable)."""
    if cron_error():
        return [f"crontab unreadable: {cron_error()}"]
    return [f"{r['id']}: matcher {r['cron']!r} matches no crontab line"
            for r in ROLES if r.get("cron") and not cron_matches(r["cron"])]


def hunt_yield():
    """What the bug hunt has actually produced — from its coverage ledger and the asks it
    filed, never from its own prose. The role was declared TEMPORARY on 2026-08-18; this is
    the measurement that decides its duration."""
    led = {}
    try:
        led = json.loads((JOURNAL / "ops" / "hunt_coverage.json").read_text()).get("areas", {})
    except Exception:
        pass
    hunts = sum(len(a.get("hunts", [])) for a in led.values())
    defects = sum(len(h.get("defects", [])) for a in led.values() for h in a.get("hunts", []))
    never = sorted(k for k, a in led.items() if not a.get("last_hunt"))
    filed = closed = 0
    try:
        rows = json.loads((DATA / "asks.json").read_text()).get("asks", [])
        mine = [r for r in rows if r.get("by") == "hunt"]
        filed = len(mine)
        closed = sum(1 for r in mine if r.get("status") == "closed"
                     and not str(r.get("decision") or "").upper().startswith("DECLINED"))
    except Exception:
        pass
    return {"hunts": hunts, "defects_with_repro": defects, "areas": len(led),
            "never_hunted": never, "asks_filed": filed, "asks_closed_fixed": closed,
            "defects_per_hunt": round(defects / hunts, 1) if hunts else None}


# ---------------------------------------------------------------- relays: where each output goes
# David, 2026-09-01: "add cleaner relays between what information is passed to where and what
# outputs go where / who reads / evaluates". Derived from the reads/writes above — the same data
# the brief stats — so the relay map cannot describe a flow the org chart does not declare.
# David and the dashboard are readers too (not roles): the sinks that make an output "read".
DAVID_READS = ["_engine/agent/data/pm_report.md", "_engine/agent/data/unknowns.md",
               "_engine/agent/data/asks.json", "_engine/agent/data/alerts.json", "phone (notify.sh)",
               "_engine/agent/journal/BOOK.md", "_engine/agent/journal/sessions/<date>_session.md",
               "_engine/agent/data/dates.json"]
DASHBOARD_READS = ["_engine/agent/data/", "_engine/agent/journal/", "_engine/agent/names/",
                   "_engine/logs/"]


def _pat(p):
    """A reads/writes entry as a regex: <date>/<role>/* wildcards, a trailing '/' or a bare
    directory matches everything beneath it."""
    p = re.sub(r"<[^>]*>", "*", str(p)).rstrip("/")
    rx = re.escape(p).replace(r"\*", "[^/]*")
    return re.compile("^" + rx + "(/.*)?$")


def _reads_of(reader_entries, artifact):
    a = re.sub(r"<[^>]*>", "*", str(artifact))
    a_rx = _pat(a)
    for r in reader_entries:
        if not str(r).startswith(("_engine", "phone", "PM launch")):
            continue
        if _pat(r).match(a) or a_rx.match(re.sub(r"<[^>]*>", "*", str(r))):
            return True
    return False


def relays():
    """[{producer, artifact, consumers:[role ids | 'david' | 'dashboard']}] for every write."""
    out = []
    for r in ROLES:
        for w in r["writes"]:
            if str(w).startswith("stdout"):
                continue
            cons = [o["id"] for o in ROLES if o["id"] != r["id"] and _reads_of(o["reads"], w)]
            if _reads_of(DAVID_READS, w) or str(w).startswith("phone"):
                cons.append("david")
            if str(w).startswith("PM launch"):
                cons.append("pm")
            if any(_pat(d).match(re.sub(r"<[^>]*>", "*", str(w))) for d in DASHBOARD_READS):
                cons.append("dashboard")
            out.append({"producer": r["id"], "artifact": w, "consumers": cons})
    return out


def unread_outputs():
    """Outputs no role, David or the dashboard reads — 'alive and mute', the Signals
    Engineer's defect class, applied to the org chart itself."""
    return [x for x in relays() if not x["consumers"]]


def _rel(p):
    return str(Path(p)) if not str(p).startswith("/") else str(Path(p).relative_to(ROOT))


def _stat(rel):
    """Freshness of a written artifact. A patterned path (`<date>_fixer.md`, `names/*/…`)
    resolves to its NEWEST match — the four ops roles' only deliverable is a dated report,
    and skipping those meant a dead role and a healthy role rendered identically on the
    org chart while the brief's header promised the opposite (ask roster.py-019, found by
    the Numbers Engineer 2026-08-20). Prose non-paths still resolve to unknown, not a guess."""
    if not rel.startswith("_engine"):
        return None
    if "*" in rel or "<" in rel:
        pat = re.sub(r"<[^>]*>", "*", rel)
        matches = [p for p in ROOT.glob(pat) if p.is_file()]
        if not matches:
            return {"exists": False}
        p = max(matches, key=lambda x: x.stat().st_mtime)
    else:
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
        live_cad = cron_cadence(r.get("cron"))
        note = r.get("cadence_note", "")
        cadence = " · ".join(x for x in (live_cad, note) if x) or "—"
        out.append({**r, "outputs": outputs, "cadence": cadence, "cadence_live": live_cad,
                    "scheduled": bool(r.get("cron")) and bool(cron_matches(r.get("cron"))),
                    "last_output_age_min": min(newest) if newest else None})
    return out


def claude_model(role_id, default="opus"):
    """Which Claude model a role thinks on. Declared here so the org chart is the single
    place a role's cost/capability is set — ops.py and vp.py read it rather than each
    carrying its own --model flag, which is how the fixer stayed on Opus for weeks after
    its work had become mechanical."""
    return claude_models(role_id, default)[0]


def charter(role_id):
    """The allowed-edit surface. For roles that own subsystems this comes from owners.py —
    ONE source of truth, so a file cannot be owned by two roles or by none (ORG_PLAN build
    step 3). The static list in ROLES is only a fallback for roles owners.py doesn't know."""
    role_id = {"fixer": "numbers"}.get(role_id, role_id)
    try:
        import owners
        surface = owners.charter(role_id)
        if surface:
            return surface
    except Exception:
        pass
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
          "| role | who | model | cadence (from `crontab -l`) | latest output |", "|---|---|---|---|---|"]
    for r in rs:
        age = _age_str(r["last_output_age_min"])
        missing = [o["path"] for o in r["outputs"]
                   if o["state"] is not None and not o["state"].get("exists")]
        note = f" · ⚠ missing: {', '.join(Path(m).name for m in missing)}" if missing else ""
        cad = r["cadence"] if r["cadence_live"] != "NOT IN CRONTAB" else f"⚠ **NOT IN CRONTAB** · {r['cadence']}"
        L.append(f"| **{r['name']}** | {r['who']} | `{r.get('model') or '—'}` | "
                 f"{cad} | {age}{note} |")
    L += [""]
    drift = cron_drift()
    if drift:
        L += ["## ⚠ Schedule drift — a role the crontab does not run", ""]
        L += [f"- {d}" for d in drift]
        L += ["", "_A role on this chart with no crontab line is the `ops.py hunt` failure class: "
              "declared, never launched. File it as an ask against `_engine/agent/roster.py` or the crontab._", ""]
    hy = hunt_yield()
    L += ["## Bug hunt — measured yield (decides its duration)", "",
          f"- {hy['hunts']} hunt(s) on record · {hy['defects_with_repro']} defect(s) with reproductions "
          f"({hy['defects_per_hunt'] if hy['defects_per_hunt'] is not None else '—'} per hunt) · "
          f"{hy['asks_filed']} ask(s) filed, {hy['asks_closed_fixed']} closed as fixed",
          f"- areas never hunted: {', '.join(hy['never_hunted']) or 'none — the first lap is complete'} "
          f"(of {hy['areas']})",
          "- rule (David, 2026-09-01): the hunt stays at two sessions a week until every area has been "
          "hunted twice; it drops to weekly when a full lap averages under one reproduced defect per hunt.", ""]
    L += ["## Relays — where each output goes (derived from this chart's reads/writes)", "",
          "| producer | artifact | read by |", "|---|---|---|"]
    for x in relays():
        who = ", ".join(x["consumers"]) if x["consumers"] else "⚠ NOBODY"
        L.append(f"| {x['producer']} | `{x['artifact']}` | {who} |")
    ur = unread_outputs()
    L += ["", (f"⚠ **{len(ur)} output(s) nobody reads** — alive and mute; either add the reader to "
              f"roster.py or stop producing it: " + ", ".join(f"`{x['artifact']}`" for x in ur))
          if ur else "_Every declared output has at least one reader._", ""]
    try:
        import reflect
        rep = reflect.report()
        st = rep.get("by_status", {})
        L += ["## Reflections — is the PM learning? (reflect.py)", "",
              f"- {rep['repeated_identical_failures_14d']} repeated identical failure(s) in 14 days "
              f"(the one metric) · pending {st.get('pending', 0)} · active {st.get('active', 0)} · "
              f"absorbed {st.get('absorbed', 0)} · frozen {st.get('frozen', 0)} · disputed {st.get('disputed', 0)} · "
              f"{rep['sessions_processed']} session(s) processed · {rep['incidents']} incident(s) gated out", ""]
    except Exception as e:
        L += ["## Reflections", "", f"- reflect.py unavailable ({type(e).__name__}) — a finding", ""]

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
        surface = charter(r["id"])
        if surface:
            L.append(f"- **May edit** (from `owners.py`): "
                     f"{', '.join(f'`{c}`' for c in surface)}")
        if r.get("escalates_to"):
            esc = r["escalates_to"]
            L.append(f"- **Escalates to:** {esc.upper()} (`asks.py escalate`)"
                     + (" — that is you." if esc == "pm" else ""))
        L.append("")

    L += ["## Your authority over this staff", "",
          "- **You may change any of their instructions, yourself.** Every role's job description is",
          "  a file: `_engine/agent/prompts/<role>.md` (vp, numbers, signals, build, coo, hunt, and",
          "  `_session_mechanics`, appended to every ops role). The tree is YOURS in `owners.py`. Edit",
          "  the text, keep every `${PLACEHOLDER}`, run `python3 _engine/agent/prompts.py check`,",
          "  commit that file alone with the reason, and record the change in your session log under",
          "  Staff. If a role is producing noise, change what you ask of it — do not work around it",
          "  session after session. NOT yours: `prompts/pm.md` (your own instructions), `MANDATE.md`,",
          "  `loop.py`, `triggers.py`, and the org chart (`roster.py`, `owners.py`) — those are asks to",
          "  David with options and a default.",
          "- **The cadence column above is the crontab, not a claim.** `roster.py cron` prints it; a",
          "  role marked NOT IN CRONTAB is declared and never launched — a finding, every time.",
          "- **The comms protocol is `_engine/agent/COMMS.md`** — one page, and it is short.",
          "  Anything one role needs from another is a row with an owner and an age, never a",
          "  paragraph with a name in it. Run `asks.py inbox <role>` first, every session.",
          "- **Asks route themselves.** `asks.py open --about <subsystem> --ask ...` resolves the",
          "  owner from `owners.py`; `--to` is the override, not the norm. A row with an owner and",
          "  an age gets worked; a paragraph in BOOK.md does not — that section had NO reader until",
          "  2026-08-19, which is why the same watchdog defect went unfixed three sessions running.",
          "- **Read the board, not just the list**: `asks.py board` groups open asks by owner and",
          "  ages them on the LAST STATE CHANGE — an ask acked and untouched for a week looks as",
          "  bad as it is. `asks.py trace <id>` shows any ask's full history when you need to know",
          "  where a request actually stalled.",
          "- **Patch requests are yours to decide.** `data/patch_requests.json` holds changes an",
          "  engineer diagnosed and verified but is not permitted to make. Approve, reject with a",
          "  reason, or escalate to David. An unanswered request is you not doing your job.",
          "- **Adding a role, or retiring one, is a proposal to David** — write it in BOOK.md under",
          "  Research wanted with the mechanism and the cost.", "",
          "## Charter note", "", CHARTER_NOTE]

    BRIEF.parent.mkdir(parents=True, exist_ok=True)
    BRIEF.write_text("\n".join(L) + _stamp())
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
    if cmd == "relays":
        for x in relays():
            print(f"{x['producer']:<9} {x['artifact']:<58} -> {', '.join(x['consumers']) or 'NOBODY'}")
        ur = unread_outputs()
        print(f"{len(relays())} relays · {len(ur)} unread output(s)")
        return 1 if ur else 0
    if cmd == "schedule":
        print(f"{'ET':<40} {'UTC':<36} {'win':<5} {'Claude tok':>10}  command")
        for utc, et, win, tok, cmd_ in schedule():
            t = "—" if tok == 0 else ("?" if tok is None else f"{tok:,}")
            print(f"{et:<40} {utc:<36} {win:<5} {t:>10}  {cmd_[:90]}")
        print("windows (UTC): " + " · ".join(f"{n} {a // 60:02d}:{a % 60:02d}–{(b // 60) % 24:02d}:{b % 60:02d} {d}" for n, a, b, d in WINDOWS))
        return 0
    if cmd == "cron":
        for r in ROLES:
            print(f"{r['id']:<10} {cron_cadence(r.get('cron')) or '(no cron line declared)':<45} "
                  f"{r.get('cadence_note', '')}")
        for d in cron_drift():
            print(f"DRIFT {d}")
        return 1 if cron_drift() else 0
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
