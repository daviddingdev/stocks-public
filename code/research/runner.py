#!/usr/bin/env python3
"""
Headless Claude Code launcher — deep research + daily portfolio recommendation.

Used by the dashboard (`/api/research`, `/api/recommend`) and by cron. Both jobs
spawn `claude -p <prompt> --dangerously-skip-permissions` on the Spark, which is
safe here because the box is private and the work is confined to ~/Stocks.

Backend requirements (each checked or handled below):
  1. Claude CLI auth — the CLI refreshes its OAuth token from
     ~/.claude/.credentials.json. If the token has been expired for a while the
     refresh token is dead too and every run 401s (root cause of the PSNY
     failure, 2026-07-13). auth_check() catches this BEFORE launching and
     returns the fix ("claude auth login") instead of a silent log line.
  2. Clean environment — a Claude session (or its Flask child) carries
     ANTHROPIC_BASE_URL / CLAUDE_* vars that point spawned CLIs at the wrong
     endpoint. clean_env() strips them so the child uses the Spark's own auth.
  3. Detached process — start_new_session so the job survives a dashboard
     restart; output streams to a log the dashboard polls.
  4. Prompt = the documented process, not an ad-hoc ask — research follows
     research/RUNBOOK.md + EVALUATION-FRAMEWORK.md; recommendations follow
     research/INVESTOR-PROFILE.md (David's goals) + live portfolio state.

CLI: runner.py research TICKER | runner.py rec | runner.py brief
"""
import datetime as dt
import json
import os
import sys
import subprocess
import time
from pathlib import Path

ROOT = Path("~/Stocks").expanduser().resolve()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from edgar_identity import user_agent  # noqa: E402  — SEC identity, config-driven

EDGAR_UA = user_agent()   # interpolated into prompts, never a literal in source
ENGINE = ROOT / "_engine"
CONF = ENGINE / "config"
LOGS = ENGINE / "logs"
REC_DIR = ENGINE / "recommendations"
CRED = Path("~/.claude/.credentials.json").expanduser()


def saved_token():
    """Long-lived subscription token from `claude setup-token`, stored in keys.json
    (gitignored) via `runner.py save-token`. Preferred over the session OAuth in
    ~/.claude/.credentials.json, which expires within hours and killed headless runs."""
    try:
        return json.loads((CONF / "keys.json").read_text()).get("claude_oauth_token", "")
    except Exception:
        return ""


def auth_check():
    """Cheap pre-flight. Returns (ok, message)."""
    if saved_token():
        return True, ""
    if not CRED.exists():
        return False, ("No Claude auth on the Spark — run `claude setup-token`, then save it with "
                       "`python3 _engine/research/runner.py save-token`.")
    try:
        c = json.loads(CRED.read_text()).get("claudeAiOauth", {})
        exp = (c.get("expiresAt") or 0) / 1000
    except Exception:
        return True, ""  # unreadable — let the run itself surface any error
    if exp and exp < time.time() - 6 * 3600:
        when = dt.datetime.fromtimestamp(exp).strftime("%b %d")
        return False, (f"Claude CLI auth expired ({when}) and can no longer refresh — run "
                       "`claude setup-token` on the Spark, then `python3 _engine/research/runner.py save-token`.")
    return True, ""


# Every spawn goes through Mission Control's race-proof wrapper: it exports the
# long-lived headless token so concurrent sessions never race the ONE rotating
# refresh token in ~/.claude/.credentials.json (box-wide auth died 08-27/28/29 that
# way — memo 2026-08-29). Wrapper missing token → execs plain claude, no change.
CLAUDE_BIN = os.path.expanduser("~/maintenance/bin/claude-headless")
if not os.path.exists(CLAUDE_BIN):
    CLAUDE_BIN = "claude"


def clean_env():
    """Environment for the child CLI: the Spark's own, minus session/harness vars."""
    drop = ("ANTHROPIC_", "CLAUDE", "AI_AGENT")
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env.setdefault("HOME", str(Path.home()))
    # FORCE the native CLI first: /usr/bin/claude is a stale root-owned npm 2.1.116 whose
    # 'opus' alias stops at 4.7 (caught 2026-08-11 — trade sessions silently ran opus-4.7).
    # setdefault never fired because cron always has a PATH.
    env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Deep research fans out to subagents and the refuter alone can run 20+ minutes.
    # `claude -p` kills background tasks at a 600s ceiling and exits — that is exactly
    # how the PANW run died (2026-08-14: "Background tasks still running after 600s;
    # terminating"), losing adversarial-review, FINAL-REPORT, lenses and card after
    # four hours of work.
    #
    # THIS WAS "0" FOR A MONTH, MEANING THE OPPOSITE OF WHAT IT SAID (fixed 2026-09-21).
    # The CLI reads the value as the ceiling ITSELF, not as a sentinel:
    #   var __=600000; function Hm(){ return a.CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS ?? __ }
    # `?? ` only fills in when the var is ABSENT, so "0" is a zero-millisecond ceiling —
    # already exceeded the instant the turn ends — and the wind-down kills every background
    # shell after a 5s grace. That is how the 2026-09-21 14:05Z PM session died: it started
    # two dossier builds, ended its turn to wait for the completion notification, and the
    # harness swept the waiter 18 seconds later ("[killed]" in the task output) instead of
    # holding the process for the pending notification the way a non-zero ceiling does.
    # 30 minutes = 3x the default, finite so a runaway background task cannot hold the
    # claudeq slot forever.
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "1800000"
    tok = saved_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def job_models(role=None):
    """ORDERED model list for a headless session; the launcher tries them in turn.

    David 2026-08-04: research and recommendation sessions are always Opus, tracking the
    most recent release (the `opus` alias). David 2026-09-22: every Claude job runs Opus 5.5
    (the PM's `best` tier included — Fable 5.1 left it); the effort differs, see job_effort().
    Tiers live in agent/roster.py CLAUDE_TIERS — one place to edit when a newer
    model lands; a role's tier comes from the ORG CHART (roster `model` field) because
    cost/capability belongs next to purpose and charter. keys.json `claude_model` still
    overrides everything, globally. No role = the `opus` tier. Either way the list comes through
    roster.claude_models(), which drops a pinned id the installed CLI cannot run — launch() uses
    the first entry and has no fallback, so the first entry must be runnable."""
    try:
        override = json.loads((CONF / "keys.json").read_text()).get("claude_model")
    except Exception:
        override = None
    if override:
        return [override]
    try:
        import sys as _s
        _s.path.insert(0, str(ENGINE / "agent"))
        import roster
        return roster.claude_models(role)     # role None → the default `opus` tier
    except Exception:
        return ["opus"]


def job_model(role=None):
    """The first choice for a role — see job_models() for the fallback order."""
    return job_models(role)[0]


def job_effort(role=None):
    """The CLI --effort for a headless session (David 2026-09-22: the PM at max, every other
    Claude job at high). From the org chart like the model — roster.claude_effort(); no role =
    the default, which is what every kind launch() starts runs at. If the org chart cannot be
    read, a named role gets `max` (never quietly below what the PM was given) and no role `high`."""
    try:
        import sys as _s
        _s.path.insert(0, str(ENGINE / "agent"))
        import roster
        return roster.claude_effort(role)
    except Exception:
        return "max" if role else "high"


_CHILDREN = {}   # pid -> Popen, so this process reaps what it spawned

# Wall-clock cap per kind, ~1.7x claudeq's est_min (research 90/finish 25/update 15) so a
# hung session dies instead of holding the queue slot — RDI ran 1,585 min against a 90-min
# estimate and stopped the queue for 12 other jobs across 19h (2026-09-07).
CAP_MIN = {"research": 150, "finish": 45, "update": 25}


# ---------- MCP scope: which sessions see the broker (brokera-1, 2026-09-22) ----------
# Until this existed every session launch() started inherited the user-scope MCP set, and
# ~/.claude.json registers brokerb-trading for ~/Stocks: research, both weekly digests, the
# board, the primer, PE, one-pagers and the industry analyst all held the live agentic-account
# order tools from 09-12 to 09-22, with only prompt text between a BROKERA-book "BUY" in the digest
# and an order on the BrokerB account — no pre-trade memo, no safety.py, no gateway intent.
# Now every session is --strict-mcp-config. The kinds that really READ the broker (transcripts:
# quotes, historicals, fundamentals, news, preview_scan) get the agent's config with every write
# tool denied (agent/broker_tools.py — the list loop.py denies too, so the two cannot drift);
# every other kind gets ops.py's no-server config. The default is NO broker: a new caller, or
# one that forgets its kind, gets nothing. Contract C36 proves this stays armed.
# Note "brief" (the daily portfolio brief) is listed and "briefs" (industry_briefs.py) is not.
BROKER_KINDS = {"research", "finish", "update", "rec", "brief", "board"}
BROKER_MCP = CONF / "agent_mcp.json"      # brokerb-trading only (loop.MCP_RESTRICT's file)
NO_MCP = CONF / "ops_mcp.json"            # no servers (ops.py's NO_MCP)


def mcp_args(broker):
    """The MCP flags for a session: broker read access with every write tool denied, or no MCP
    server at all. Both are --strict-mcp-config, so the user-scope set never loads."""
    if broker:
        sys.path.insert(0, str(ENGINE / "agent"))
        from broker_tools import ALL_WRITE_TOOLS, PREVIEW_TOOLS, disallowed_arg
        return ["--strict-mcp-config", "--mcp-config", str(BROKER_MCP),
                "--disallowedTools", disallowed_arg(ALL_WRITE_TOOLS + PREVIEW_TOOLS)]
    return ["--strict-mcp-config", "--mcp-config", str(NO_MCP)]


def _ensure_no_mcp():
    """ops.py's idiom: the no-server config is gitignored, so recreate it rather than let a
    missing file kill the session."""
    if not NO_MCP.exists():
        NO_MCP.parent.mkdir(parents=True, exist_ok=True)
        NO_MCP.write_text(json.dumps({"_doc": "ops sessions get NO MCP servers — no broker, "
                                              "no external tools beyond the box", "mcpServers": {}}, indent=1))


def _utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _slot_skip(job, kind, msg):
    """A fixed-time cron job (company cards, industry analyst, PE, industry briefs, primer, board)
    that did not get the Claude slot. These jobs are invisible to the queue — claudeq cannot keep
    their start time free — so a teardown that runs to its 150-min cap can hold the slot through
    their whole wait, and nothing re-files them (09-22: the 04:30Z one-pagers gave up at 05:30:00,
    five seconds before research:COO released). The proper fix is queue kinds for these jobs in
    Mission Control's claudeq.py; until then a skip is said out loud, once: a line in the cron log
    and one push (actionable; lean mode routes it to the evening digest)."""
    line = f"[runner.py] {_utc()} SKIPPED {job} ({kind or job}): {msg} — it did not run and nothing re-files it"
    print(line, flush=True)
    try:
        sys.path.insert(0, str(ENGINE))
        import notify
        try:                                   # a person reads this: ET first, UTC alongside
            sys.path.insert(0, str(ENGINE / "agent"))
            import asof
            when = asof.fmt()
        except Exception:
            when = _utc()
        notify.push(f"Stocks · Claude job skipped: {job}",
                    f"{when} — {job} did not get the Claude slot ({msg}); it did not run and nothing re-files it",
                    tier="actionable", kind=None)
    except Exception:
        pass
    return {"ok": False, "msg": msg}


def pid_file(log_path):
    return Path(str(log_path) + ".pid")


def _watchdog(pid, log_path, cap_min):
    """Detached: kill the session's whole process group if it outlives cap_min, the vp.py
    subprocess-timeout idiom adapted for runner.py's detached (start_new_session) spawns,
    which nothing else is blocked on and so nothing else can time out."""
    try:
        subprocess.Popen(
            ["bash", "-c",
             f"sleep {int(cap_min * 60)}; "
             f"if kill -0 {int(pid)} 2>/dev/null; then "
             f"echo '[runner.py] TIMEOUT: killed after {cap_min}m wall-clock cap' >> {log_path}; "
             f"kill -TERM -{int(pid)} 2>/dev/null; sleep 5; kill -KILL -{int(pid)} 2>/dev/null; "
             f"fi"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _kill_group(p):
    """TERM the session's whole process group (start_new_session: pgid == pid, so the
    claude-headless cap watcher goes with it), KILL after 10s, and reap it."""
    import signal
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        return
    try:
        p.wait(timeout=10)
    except Exception:
        try:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=5)
        except Exception:
            pass


def launch(prompt, log_path, job=None, kind=None, sub=None, est_min=None, wait_s=0):
    """Spawn ONE headless Claude session. `prompt` may be a string or a CALLABLE — a callable is
    resolved after the slot is acquired, so a queued job picks its work when it starts. ONE AT A TIME (claudeq, David 2026-09-04): a
    caller that names its `job` is a fixed cron launch (board, primer) — it waits up to
    wait_s for the Claude slot and then holds it; a caller that does not is being
    dispatched by claudeq.tick(), which already holds the slot for it. Either way a
    detached watcher ticks the queue when the session exits, so the next job starts
    within seconds instead of at the next cron minute.

    MCP scope: `kind` in BROKER_KINDS gets broker reads with every write tool denied; anything
    else — no kind included — gets no MCP server at all (see mcp_args)."""
    ok, msg = auth_check()
    if not ok:
        return {"ok": False, "msg": msg}
    if job:
        import claudeq
        if not claudeq.wait_free(wait_s):
            h = claudeq._read(claudeq.HOLDER) or {}
            return _slot_skip(job, kind, f"Claude slot busy after {wait_s}s: {h.get('job', '?')} still running")
    # A job that WAITED for the slot must choose its work now, not when it was filed. Pass a
    # callable and it is resolved here, after the wait. Three one-pager sessions chained on
    # 2026-09-21 each computed their ten names up front, so the second rewrote six the first had
    # already written — six minutes of Opus on work that was already done.
    if callable(prompt):
        prompt = prompt()
    if not prompt:
        return {"ok": False, "msg": "nothing to do: the prompt builder returned empty"}
    # a missing broker config degrades to no broker (the session still runs, on yfinance/EDGAR),
    # never to the user-scope MCP set
    broker = kind in BROKER_KINDS and BROKER_MCP.exists()
    if not broker:
        _ensure_no_mcp()
    model = job_model()
    effort = job_effort()      # every kind here is a non-PM job: `high` (roster.CLAUDE_EFFORT)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate, write the header, then hand the child an APPEND descriptor. With plain "w" the
    # child's descriptor sat at offset 0 without O_APPEND: the watchdog appended its TIMEOUT
    # marker with >>, and claude -p then flushed its final text from offset 0 over it — no log
    # anywhere held the marker, so log_error could never report a cap kill (VRRM and COO were
    # both killed at exactly 150 min on 09-19/22 and read as clean finishes). The header line is
    # the model and effort the session actually ran on, which nothing recorded before.
    log_path.write_text(f"[runner.py] {_utc()} model {model} · effort {effort} · "
                        f"{'broker read-only (writes denied)' if broker else 'no MCP servers'} · "
                        f"{kind or job or 'session'}\n")
    log = open(log_path, "a")
    try:
        p = subprocess.Popen([CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions",
                              "--model", model, "--effort", effort] + mcp_args(broker),
                             cwd=str(ROOT), stdout=log, stderr=log,
                             start_new_session=True, env=clean_env())
    except Exception as e:
        return {"ok": False, "msg": f"launch error: {e}"[:160]}
    # Record the pid. Without it "is this still running?" was answered by log mtime,
    # which cannot tell a working run from a dead one — a finished/crashed job kept
    # reporting "in progress" for a full hour, then silently became "idle".
    _CHILDREN[p.pid] = p
    try:
        pid_file(log_path).write_text(str(p.pid))
    except Exception:
        pass
    took, why = True, ""
    try:
        import claudeq
        if job:
            took, why = claudeq.take(job, p.pid, kind or job, sub, est_min, log_path)
        if took:
            claudeq.watcher(p.pid)
    except Exception:
        pass
    if not took:
        # PLUMB-4: wait_free() only SAW a free slot, it did not claim it. Between that and take()
        # the tick (or another waiter) started its own session, and take() said so. Two sessions
        # on the one credential is the thing the queue exists to prevent, so ours goes — before
        # it has done any work — and the skip is reported like a timeout.
        _kill_group(p)
        _CHILDREN.pop(p.pid, None)
        try:
            pid_file(log_path).unlink()
        except Exception:
            pass
        try:
            with open(log_path, "a") as fh:
                fh.write(f"[runner.py] {_utc()} killed at launch: lost the Claude slot ({why})\n")
        except Exception:
            pass
        return _slot_skip(job, kind, f"lost the Claude slot at launch — {why}")
    if kind in CAP_MIN:
        _watchdog(p.pid, log_path, CAP_MIN[kind])
    return {"ok": True, "pid": p.pid, "log": str(log_path)}


def reap():
    """Poll every child we spawned so finished ones leave the process table. run_state()
    reaps as a side effect, but the dashboard's status route returns early once
    FINAL-REPORT.md exists — so a run that COMPLETED was never polled and sat <defunct>
    (observed 2026-08-14: PANW finished 06:36Z, still a zombie at 16:57Z). Call this
    unconditionally, not only on the not-done path."""
    for pid, proc in list(_CHILDREN.items()):
        if proc.poll() is not None:
            _CHILDREN.pop(pid, None)


def run_state(log_path):
    """'alive' | 'exited' | 'unknown' for a launched run, reaping our own child on the
    way. The dashboard is the parent and never called wait(), so finished runs sat in
    its process table as <defunct> — a zombie is EXITED, not running (PANW showed
    'researching…' for an hour against a defunct pid)."""
    try:
        pid = int(pid_file(log_path).read_text().strip())
    except Exception:
        return "unknown"
    p = _CHILDREN.get(pid)
    if p is not None:
        return "alive" if p.poll() is None else "exited"   # poll() reaps
    try:  # spawned before a dashboard restart — ask the kernel
        st = Path(f"/proc/{pid}/stat").read_text()
        return "exited" if st.rsplit(")", 1)[1].split()[0] == "Z" else "alive"
    except Exception:
        return "exited"


# Hard stops that are not "API Error" but end the run just as dead. The 600s one is
# the background-task ceiling that killed PANW; clean_env now sets it to 0, and this
# stays so any recurrence is reported instead of read as ordinary output.
FATAL_MARKS = [
    ("TIMEOUT: killed after", "Run hit its wall-clock cap and was killed to free the queue slot — "
                              "re-file it (a hang, not a completed run)."),
    ("Background tasks still running", "Run stopped early: it hit the 600s background-task ceiling "
                                       "and terminated with deliverables missing."),
    ("Execution error", "Run failed: the CLI reported an execution error."),
    ("Credit balance is too low", "Run failed: Claude credit balance too low."),
    ("Invalid API key", "Run failed: invalid API key — re-save the token with `runner.py save-token`."),
]


def log_error(log_path):
    """If a run's log ends in an auth/API/hard-stop failure, return an actionable message."""
    if not log_path.exists():
        return ""
    try:
        tail = log_path.read_text(errors="ignore")[-4000:]
    except Exception:
        return ""
    try:
        import claudeq
        reset = claudeq.limit_reset(tail[-600:])
    except Exception:
        reset = None
    if reset:
        return (f"Run died on the shared Claude usage limit (resets {reset:%H:%MZ}) — the queue "
                f"re-files it for after the reset; nothing here is done.")
    if "401" in tail and "authentication" in tail.lower():
        return "Run failed: Claude CLI auth expired — run `claude auth login` on the Spark, then retry."
    if "API Error" in tail:
        line = [l for l in tail.splitlines() if "API Error" in l]
        return ("Run failed: " + line[-1][:140]) if line else ""
    for mark, msg in FATAL_MARKS:
        if mark in tail:
            return msg
    return ""


# ---------- deep research (the OABI teardown) ----------
def research_log(tk):
    return LOGS / f"research_{tk.upper()}.log"


def research_prompt(tk):
    tk = tk.upper()
    return (
        f"research {tk} — run the deep-research teardown exactly as specified in "
        f"~/Stocks/_engine/research/RUNBOOK.md, applying the evaluation methodology in "
        f"~/Stocks/_engine/research/EVALUATION-FRAMEWORK.md (gates, five pillars, asymmetry, kill-the-thesis). "
        f"Steps: (0) FIRST run `python3 ~/Stocks/_engine/research/evidence.py {tk}` — it builds "
        f"research/_evidence/ (XBRL fact series in facts.json, text-extracted key filings, sections.json "
        f"offsets, INDEX.md navigation map with local-model reading notes), THEN run "
        f"`python3 ~/Stocks/_engine/research/shelf.py {tk}` (run it in the FOREGROUND and wait for it to exit; NEVER poll for it with `pgrep -f` on the ticker or the script name — that pattern matches this session's own command line, the wait never ends, and the queue froze 23h on 2026-09-06 exactly so) — it puts the agent book's existing work on the "
        f"desk as research/_evidence/SHELF.md + shelf/ (the XBRL fincard with formulas and the mechanical "
        f"reverse-DCF/DCF grid, dossier-verified contractual terms and deal filings, the Bench's verbatim-verified "
        f"narrative-vs-filing reads, insider cluster + Form 4 rows, 13D subjects, scout and cannibal hits). Create any missing "
        f"~/Stocks/<CompanyName>-{tk}/ dirs (filings/ financials/ research/ analysis/, Name-TICKER convention "
        f"like OmniAB-OABI). EVERY research agent you spawn must be told to read SHELF.md + INDEX.md + facts.json FIRST "
        f"and cite pack/shelf files where they suffice — every financial figure comes from shelf/fincard.json with "
        f"its XBRL tag and period unless the card lacks it; pulling additional raw EDGAR material stays allowed and "
        f"expected whenever the pack is thin (the pack narrows search, it never limits it); (1) fan out parallel research agents in one "
        f"message — governance/insiders/ownership (starts from shelf/insiders.json, sc13d.json, terms.json), product/pipeline/business, competitive/market, IP/legal/moat, "
        f"financials/capital-structure (starts from shelf/fincard.json) — each writing a sourced research/<topic>.md; (2) reconcile, hunt the "
        f"load-bearing contradiction, verify value actually accrues to shareholders, and build the explicit bear "
        f"case; (2.5) REQUIRED, per RUNBOOK §2.5 — fire ONE fresh context-isolated adversarial-refuter agent that "
        f"reads ONLY research/*.md and the primary record (filings/, EDGAR, XBRL, transcripts — NEVER analysis/), "
        f"is HANDED shelf/bench.json as pre-found primary-source contradictions to confirm or overturn against the quoted document, "
        f"attempts to overturn each load-bearing claim with a primary-source citation, and writes "
        f"research/adversarial-review.md with per-claim overturned/hardened/unchanged verdicts (unsourced doubt = "
        f"unchanged; hardened must carry the strongest counter-evidence found); the FINAL-REPORT must cite how "
        f"each pillar survived the refuter and lenses.json overall.summary must end with "
        f"'(N overturned / M hardened of K claims — see adversarial-review.md)', and after writing it update the "
        f"adversarial-verifier-research row in ~/memos/LEDGER.md from implemented to verified with the counts "
        f"(first-run evidence, per the ledger's 2026-08-10 amendment); "
        f"(3) synthesize analysis/soft-research-dossier.md, a fit-for-purpose valuation (DCF+reverse-DCF if "
        f"cash-generative, rNPV/sum-of-parts if optionality — never force the wrong one, always add comps; START from "
        f"the shelf fincard's mechanical ruler and state your bear/base/bull as a judgment delta from it) with "
        f"bear/base/bull per share vs. price, analysis/trade-playbook.md (sizing, tranches, sell ladder, "
        f"thesis-break triggers), analysis/FINAL-REPORT.md, and analysis/lenses.json using the same schema as "
        f"~/Stocks/OmniAB-OABI/analysis/lenses.json so the dashboard renders it, WITH the v0.3 verdict fields in "
        f"`overall` per EVALUATION-FRAMEWORK.md §Verdict: verdict (Buy now | Buy below | Follow | Pass), bar "
        f"(catalyst | compounder, with one clause why), buy_below (from the bar's formula — base/2 for catalyst, "
        f"min(base/1.30, bear/0.75) for compounder; null ONLY for a Pass, naming the failed gate), bear/base/bull "
        f"per share, price and as_of the verdict was struck at, and signal = the verdict text plus the price for "
        f"Buy below (e.g. 'Buy below $40'); 'Neutral' is retired — a verdict without buy_below is a Pass with a "
        f"named failed gate, nothing else; plus analysis/card.json "
        f"(the one-page thesis card, same schema as OmniAB-OABI/analysis/card.json — thesis sentence, state, "
        f"now/later actions, milestones, ladder, kill triggers; 2-4 items per list). Be honest, argue both "
        f"sides, and report gaps plainly — this informs David's decision, it is not a pitch. "
        f"BEFORE YOUR FINAL MESSAGE: stop every background agent still running (a pillar agent left "
        f"running after the synthesis is written burns the one box-wide Claude slot for nothing — VRRM's "
        f"ran 43 minutes past the answer on 2026-09-19 until the wall-clock cap killed the session), and "
        f"name in FINAL-REPORT.md's gaps any agent whose output you did not wait for."
    )


def finish_applicable(tk):
    """A teardown that collected everything and died before synthesis: research/*.md incl.
    the adversarial review exist, analysis/FINAL-REPORT.md does not. CNNE (2026-09-04), ETD
    and MRP (08-31) all died this way on the usage limit — two hours of collection on disk
    and no verdict. Finishing is a ~25-min synthesis session, not a two-hour re-teardown."""
    cd = company_dir(tk)
    if not cd:
        return False
    return ((cd / "research" / "adversarial-review.md").exists()
            and not (cd / "analysis" / "FINAL-REPORT.md").exists())


def finish_prompt(tk):
    """Step (3) of the teardown ONLY, over the research already on disk. The step-3 text
    is sliced out of research_prompt() so the two can never drift."""
    tk = tk.upper()
    full = research_prompt(tk)
    step3 = full[full.index("(3) synthesize"):]
    cd = company_dir(tk)
    return (
        f"finish {tk} — FINISH MODE for an existing deep-research teardown in ~/Stocks/{cd.name}/. The "
        f"collection steps are DONE and on disk: research/*.md (every pillar), research/adversarial-review.md "
        f"(the refuter's per-claim verdicts), research/_evidence/ (facts.json, INDEX.md, sections.json, the "
        f"text-extracted filings) and the model files under financials/. Do NOT re-run evidence.py, do NOT "
        f"re-spawn pillar agents, do NOT re-run the refuter — a session doing that died on the usage limit "
        f"after two hours with nothing in analysis/. If research/_evidence/SHELF.md is missing, run ONLY "
        f"`python3 ~/Stocks/_engine/research/shelf.py {tk}` (run it in the FOREGROUND and wait for it to exit; NEVER poll for it with `pgrep -f` on the ticker or the script name — that pattern matches this session's own command line, the wait never ends, and the queue froze 23h on 2026-09-06 exactly so) first. Then READ, in this order: SHELF.md, "
        f"shelf/fincard.json, research/adversarial-review.md, every research/*.md, financials/*. Where the "
        f"refuter OVERTURNED a claim, the synthesis must carry the overturned version, never the original. "
        f"Apply ~/Stocks/_engine/research/EVALUATION-FRAMEWORK.md (gates, five pillars, asymmetry, "
        f"kill-the-thesis) and RUNBOOK.md §3. Now do step {step3}"
    )


def launch_finish(tk, now=False):
    tk = tk.upper()
    if not finish_applicable(tk):
        return {"ok": False, "msg": f"{tk} is not finishable: needs research/adversarial-review.md and no FINAL-REPORT yet."}
    if run_state(research_log(tk)) == "alive":
        return {"ok": False, "msg": f"Research on {tk} is already running."}
    if not now:
        import claudeq
        return claudeq.enqueue("finish", {"tk": tk}, by="runner")
    return launch(finish_prompt(tk), research_log(tk), kind="finish")


def gate_check(tk):
    """STRATEGY-PROPOSAL-v3 §7/§8: a name that hasn't passed the coded pre-teardown gate
    (research/gate.py) doesn't earn a teardown — the most expensive Claude job this book
    files. Reads the per-name gate.json gate.py already writes (company dir or agent/names
    shelf); missing or 'fail' returns the reason as a string, the failed gate's own detail
    line so the refusal is checkable, not just asserted. None means clear to file."""
    tk = tk.upper()
    import gate
    src = gate._company_source(tk) or gate._shelf_source(tk)
    gate_path = (src["out"] / "gate.json") if src else None
    g = json.loads(gate_path.read_text()) if gate_path and gate_path.exists() else None
    if not g:
        return f"{tk}: no gate.json on file — run `gate.py {tk}` before filing a teardown"
    if g.get("verdict") == "fail":
        failed = next((x for x in g.get("gates", []) if x.get("gate") == g.get("failed_gate")), {})
        return f"{tk}: gate FAIL ({g['failed_gate']}) — {failed.get('detail', '')}"
    return None


# ---------- funnel accounting for the 'teardown' stage (runner.py-214) ----------
# scout.py-172 gave every earlier stage a funnel_counts.jsonl row; 'teardown:filed' had
# none, so the roster brief's §7 quota table showed "NO ROWS — stage never reports" in a
# week CNDT was filed and consumed. A filed teardown is asynchronous (queued, dispatched
# minutes to hours later, runs up to CAP_MIN["research"] minutes) so 'teardown:done' can't
# fire inline — it's resolved later by reconcile_teardowns() against the two ground
# truths that actually exist: lenses.json landing, or the claudeq job for that ticker
# ending in release/fail.
TEARDOWN_PENDING = ENGINE / "agent" / "data" / "teardown_pending.json"


def _load_pending_teardowns():
    try:
        return json.loads(TEARDOWN_PENDING.read_text())
    except Exception:
        return {}


def _save_pending_teardowns(d):
    TEARDOWN_PENDING.parent.mkdir(parents=True, exist_ok=True)
    TEARDOWN_PENDING.write_text(json.dumps(d, indent=2))


def _record_teardown_filed(tk):
    sys.path.insert(0, str(ENGINE / "agent"))
    from feeds import funnel_record
    funnel_record("teardown:filed", 1, 1, ticker=tk)
    d = _load_pending_teardowns()
    d[tk] = time.time()
    _save_pending_teardowns(d)


def _teardown_job_died(tk, since):
    """A release/fail event for this ticker's research or finish job, after it was
    filed — read-only against claudeq's own event log (coo-owned; we never write it)."""
    try:
        import claudeq
        lines = claudeq.EVENTS.read_text(errors="ignore").splitlines()
    except Exception:
        return False
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("at", 0) < since or e.get("ev") not in ("release", "fail"):
            continue
        if e.get("job") in (f"research:{tk}", f"finish:{tk}"):
            return True
    return False


def reconcile_teardowns():
    """Runs opportunistically on every runner.py entry point: for each ticker filed but
    not yet resolved, check whether lenses.json landed (success) or the job died without
    one (failure) and emit the matching 'teardown:done' row. Idle (no-op) once caught up."""
    d = _load_pending_teardowns()
    if not d:
        return
    sys.path.insert(0, str(ENGINE / "agent"))
    from feeds import funnel_record
    changed = False
    for tk, since in list(d.items()):
        cd = company_dir(tk)
        lenses = (cd / "analysis" / "lenses.json") if cd else None
        if lenses and lenses.exists() and lenses.stat().st_mtime >= since:
            funnel_record("teardown:done", 1, 1, ticker=tk)
            del d[tk]
            changed = True
        elif _teardown_job_died(tk, since):
            funnel_record("teardown:done", 1, 0, ticker=tk)
            del d[tk]
            changed = True
    if changed:
        _save_pending_teardowns(d)


def launch_research(tk, now=False, full=False):
    """Deep teardown. Files a queue job (claudeq) unless dispatched by the queue itself
    (now=True). One per ticker: a live run refuses a second launch — OABI launched twice
    nine seconds apart on 2026-09-04 and one copy died at 94s for nothing. A teardown that
    only lacks its synthesis is FINISHED, not redone, unless full=True."""
    tk = tk.upper()
    reconcile_teardowns()
    if run_state(research_log(tk)) == "alive":
        return {"ok": False, "msg": f"Research on {tk} is already running."}
    reason = gate_check(tk)
    if reason:
        return {"ok": False, "msg": reason}
    # Count a teardown ONCE, when it is filed. The queue dispatches it later as
    # launch_research(tk, now=True), which used to record it again — every teardown showed twice
    # against FUNNEL_QUOTA['teardown:filed'] (VRRM 09-18 + 09-19, COO 09-21 + 09-22) and the
    # second row reset the pending timestamp to the dispatch time. A --now launch that was never
    # filed still counts once.
    if not (now and tk in _load_pending_teardowns()):
        _record_teardown_filed(tk)
    if not full and finish_applicable(tk):
        return launch_finish(tk, now=now)
    if not now:
        import claudeq
        return claudeq.enqueue("research", {"tk": tk}, by="runner")
    return launch(research_prompt(tk), research_log(tk), kind="research")


# ---------- focused research update (refresh, not re-teardown) ----------
def company_dir(tk):
    for d in ROOT.iterdir():
        if d.is_dir() and not d.name.startswith(("_", ".")) and d.name.split("-")[-1].upper() == tk.upper():
            return d
    return None


def update_log(tk):
    return LOGS / f"update_{tk.upper()}.log"


def update_prompt(tk, folder, report_date):
    tk = tk.upper(); today = dt.date.today().isoformat()
    return (
        f"update {tk} research — a FOCUSED refresh of an existing dossier, not a full teardown. "
        f"The research in ~/Stocks/{folder}/ is dated {report_date}; catch it up to {today}.\n"
        f"First read what exists: analysis/FINAL-REPORT.md, analysis/trade-playbook.md (if present), "
        f"analysis/lenses.json, and skim the research/ files for the thesis and its named triggers. "
        f"Also read ~/Stocks/_engine/journal/notes.json and decisions.json for entries tagged {tk} — "
        f"David's own thinking on this name; engage with it in the update (agree, push back, answer questions).\n"
        f"LADDER DISCIPLINE (per _engine/INFORMATION-ARCHITECTURE.md): if the valuation's bear/base/bull has "
        f"materially moved on FUNDAMENTALS (new guidance, readout, deal — never price alone), RE-DERIVE the "
        f"trade-playbook's sell ladder and add zones from the CURRENT valuation in the playbook file itself, "
        f"dating the revision and stating the old vs new rungs and why. Where lots are short-term for capital "
        f"gains (see /api/lots dates; >1yr = long-term), note it: tax tilts WHERE in a zone discretionary rungs "
        f"sit, but never vetoes risk-driven rungs (thesis-break or pre-binary de-risking).\n"
        f"Run `python3 ~/Stocks/_engine/research/shelf.py {tk}` (run it in the FOREGROUND and wait for it to exit; NEVER poll for it with `pgrep -f` on the ticker or the script name — that pattern matches this session's own command line, the wait never ends, and the queue froze 23h on 2026-09-06 exactly so) and read research/_evidence/SHELF.md — the "
        f"agent book's XBRL fincard (current price, TTM figures, mechanical reverse-DCF/DCF grid), verified "
        f"contractual terms, Bench reads, insider and 13D rows; quote its numbers with their tags instead of "
        f"re-deriving them.\n"
        f"Then gather ONLY what is new since {report_date}: SEC filings via curl on EDGAR "
        f"(UA '{EDGAR_UA}') — especially 10-Q/8-K and earnings releases; the earnings call "
        f"transcript or press release if the company just reported; company news; current price and market "
        f"cap (yfinance via _engine/.venv).\n"
        f"Write analysis/updates/update-{today}.md (note the updates/ subfolder) with exactly these sections:\n"
        f"1. **What changed since {report_date}** — facts and numbers vs. the prior model's expectations;\n"
        f"2. **Thesis impact** — which pillars/gates moved; is the variant perception intact, strengthened, or broken;\n"
        f"3. **Valuation delta** — bear/base/bull revisions only if warranted, with the changed assumption named;\n"
        f"4. **Action** — evaluate against the trade-playbook's pre-defined triggers (sell ladder, thesis-break, "
        f"add conditions) and state specifically: hold / add (size) / trim (size) / exit — plus validity window "
        f"and stale-condition (absolute dates); and RE-SCORE under EVALUATION-FRAMEWORK.md §Verdict (v0.3): name "
        f"the bar (catalyst | compounder) and why, show the arithmetic for buy_below from this update's "
        f"bear/base/bull at today's price, and state the verdict (Buy now | Buy below $X | Follow | Pass);\n"
        f"5. **Next catalysts** — dated and verified (state how verified).\n"
        f"If the playbook's price ladder or add/trim levels changed (or aren't there yet), merge them into "
        f"~/Stocks/_engine/config/triggers.json under price_levels as "
        f"{{\"{tk}\": {{\"above\": X, \"below\": Y, \"note\": \"...\"}}}} — preserve other tickers' entries; "
        f"the trigger engine pings David's phone at these levels.\n"
        f"Then: REWRITE analysis/card.json — the one-page thesis card the dashboard leads with. Schema: "
        f'{{"as_of","thesis" (one sentence),"state" (working|strengthening|intact|stressed|broken),"state_note",'
        f'"now" [immediate actions],"later" [long-term actions],"milestones" [{{"date","label"}}],'
        f'"ladder" [{{"zone","action","status"}} — mark resting orders LIVE],"kill" [top thesis-break triggers]}}. '
        f"Keep every list to 2-4 tight items — this is the ONLY thing David reliably reads; the card must stand alone.\n"
        f"Also: REWRITE analysis/lenses.json `overall` with the v0.3 verdict fields (verdict, bar, buy_below, "
        f"bear, base, bull, price, as_of, signal = verdict text plus price for Buy below, confidence, summary) and "
        f"update the lenses if the read changed; prepend a line "
        f"'> **Updated {today}** — see [update-{today}.md](updates/update-{today}.md)' under the title of "
        f"FINAL-REPORT.md; and update this ticker's next_catalyst in ~/Stocks/_engine/config/positions.json "
        f"if it changed. Be honest — if nothing material changed, say so in one page and stop (but still "
        f"refresh the card's as_of and any dated items)."
    )


def _held():
    try:
        pos = json.loads((CONF / "positions.json").read_text())
        return [t for t, m in pos.items() if (m.get("shares") or 0) > 0]
    except Exception:
        return []


def _ntfy_topic():
    try:
        return json.loads((CONF / "triggers.json").read_text()).get("ntfy_topic", "")
    except Exception:
        return ""


def launch_update(tk, now=False):
    tk = tk.upper()
    cd = company_dir(tk)
    if not cd:
        return {"ok": False, "msg": f"No research folder for {tk} — run a full Deep research instead."}
    today_f = f"update-{dt.date.today().isoformat()}.md"
    if (cd / "analysis" / "updates" / today_f).exists() or (cd / "analysis" / today_f).exists():
        return {"ok": False, "msg": f"{tk} already has an update dated today."}
    lg = update_log(tk)
    if run_state(lg) == "alive":
        return {"ok": False, "msg": f"An update for {tk} is already running."}
    if not now:
        import claudeq
        return claudeq.enqueue("update", {"tk": tk}, by="runner")
    rep = cd / "analysis" / "FINAL-REPORT.md"
    rdate = dt.date.fromtimestamp(rep.stat().st_mtime).isoformat() if rep.exists() else "unknown"
    prompt = update_prompt(tk, cd.name, rdate)
    if tk in _held():
        # Route the session's own push through Mission Control's choke point, not a raw
        # curl — that is what lets box-wide tiering see it (PROJECT_STANDARDS §1).
        prompt += (f"\nFINALLY — {tk} is a HELD position, so notify David that the refresh landed: "
                   f"~/maintenance/bin/notify.sh stocks 'Research updated: {tk}' "
                   f"'<one line: your section-4 action, e.g. HOLD — thesis intact, next catalyst <date>>'")
    return launch(prompt, update_log(tk), kind="update")


def launch_autorefresh():
    """Cron entry: refresh any HELD name with research the morning after it reports.
    Checks Finnhub's earnings calendar for yesterday (AMC) and today (BMO)."""
    import urllib.request
    try:
        key = json.loads((CONF / "keys.json").read_text()).get("finnhub", "")
    except Exception:
        key = ""
    if not key:
        return {"ok": False, "msg": "no finnhub key"}
    yday = (dt.date.today() - dt.timedelta(1)).isoformat()
    today = dt.date.today().isoformat()
    launched, results = [], []
    for tk in _held():
        if not company_dir(tk):
            continue
        url = (f"https://finnhub.io/api/v1/calendar/earnings?from={yday}&to={today}"
               f"&symbol={tk}&token={key}")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                cal = json.loads(r.read()).get("earningsCalendar", [])
        except Exception:
            continue
        hit = any(e.get("date") == yday or (e.get("date") == today and e.get("hour") == "bmo") for e in cal)
        if hit:
            res = launch_update(tk)
            results.append(f"{tk}: {'launched' if res.get('ok') else res.get('msg')}")
            if res.get("ok"):
                launched.append(tk)
    return {"ok": True, "launched": launched, "detail": results}


# ---------- daily portfolio recommendation ----------
def rec_path(day=None):
    day = day or dt.date.today().isoformat()
    return REC_DIR / f"rec_{day}.md"


def rec_log():
    return LOGS / "recommendation.log"


def standing_feedback():
    """David's standing instructions, entered via the dashboard's /recommendation page."""
    f = REC_DIR / "feedback.json"
    try:
        return json.loads(f.read_text()) if f.exists() else []
    except Exception:
        return []


def rec_prompt():
    today = dt.date.today().isoformat()
    fb = standing_feedback()
    fbtxt = ""
    if fb:
        lines = "\n".join(f"- [{e.get('date','')}] {e.get('msg','')}" for e in fb)
        fbtxt = ("STANDING FEEDBACK from David (entered via the dashboard — binding, oldest first):\n"
                 f"{lines}\n"
                 "Honor every item above. If an item declines a past recommendation, do NOT re-recommend it "
                 "unless facts have materially changed — and then explicitly acknowledge his note and say what "
                 "changed. If an item asks a question or sets a preference, address it in section 6.\n")
    return (
        f"You are David's investment strategist. Write this week's portfolio digest to "
        f"~/Stocks/_engine/recommendations/rec_{today}.md (markdown, start with '# Claude recommendation — {today}').\n"
        f"Cadence context: David is a weekly-cadence operator with <=5 hrs/week for this — write for one focused "
        f"weekly read, not a daily trader.\n"
        f"THE PROFILE IS LAW: ~/Stocks/_engine/research/INVESTOR-PROFILE.md — especially its AMENDMENTS section — "
        f"overrides anything in this prompt and any prior digest. Do not assume constraints from earlier digests "
        f"still hold; re-derive them from the profile at every run. Non-negotiables that never change: never "
        f"propose actions touching the locked private holding in external.json; human approval on every trade; "
        f"judge the process (thesis quality, pre-mortems, journal) over short-run returns.\n"
        + fbtxt +
        f"First read, in order: ~/Stocks/_engine/research/INVESTOR-PROFILE.md (David's goals — the lens for "
        f"everything), ~/Stocks/_engine/journal/notes.json and decisions.json (David's OWN dated thinking and "
        f"decision log — context to engage with, not instructions; the feedback list above is what binds), "
        f"~/Stocks/_engine/config/positions.json, account.json, watchlist.txt, external.json, "
        f"the newest ~/Stocks/_engine/candidate-boards/board_*.md, every analysis/FINAL-REPORT.md and "
        f"analysis/lenses.json under ~/Stocks/*/, and ~/Stocks/_engine/research/EVALUATION-FRAMEWORK.md. "
        f"Pull live prices for holdings and watchlist (yfinance via _engine/.venv, or curl) so numbers are current.\n"
        f"NEWS LAYER (additive, added 2026-08-11): also read ~/Stocks/_engine/agent/data/news_brief.md — the "
        f"local-model materiality-scored top news items of the last ~30h (check its generated timestamp; on "
        f"Mondays it is Friday's brief, weekend news is thin). Treat it as a pre-filtered ranking signal ONLY, "
        f"never as the news record: the raw ~/Stocks/_engine/agent/data/feed.json stays authoritative — go to it "
        f"directly whenever the brief flags something, looks stale, or a holding likely had news the brief missed.\n"
        f"BROKERA STAFF BRIEF (additive, added 2026-08-24 — closes the fourth-consecutive-digest news gap): read "
        f"~/Stocks/_engine/briefs/jpm_brief.md — the CODED staff brief over David's OWN universe (holdings + "
        f"watchlist + researched names): quotes, ranked EDGAR filings, earnings dates, and what the BrokerB "
        f"org's offline machinery (fincards, bench reads, scout rows, 13D/delisting radar) produced on his names. "
        f"No model wrote it. Check its 'Feed as of' stamp; if older than ~24h, regenerate first — it is cheap "
        f"code: `python3 ~/Stocks/_engine/research/brokera.py refresh && python3 ~/Stocks/_engine/research/brokera.py "
        f"brief`. Treat it as prepared evidence with pointers — drill into the linked primary documents for "
        f"anything a recommendation depends on; the brief is never the source.\n"
        f"DECISION LEDGER (added 2026-08-24 — decisions.json now exists and is the contract): after writing the "
        f"digest, register EVERY actionable numbered recommendation from section 3 as a decision row via "
        f"`python3 ~/Stocks/_engine/research/brokera.py propose --source digest --ticker TK --ask '<one sentence>' "
        f"--options 'A (recommended): ... / B: ...' --default '<what happens if David does nothing>' "
        f"--valid-until YYYY-MM-DD` (match the rec's validity window; do-nothing recs need no row). If a rec "
        f"re-prices, resizes or replaces a row that is still OPEN for the same ticker, add `--supersedes <old id>` "
        f"so the old row closes as superseded — never leave David two open asks on one name (09-21: VSNT $36.00 "
        f"and $35.25 both open). In section 6, "
        f"score prior weeks from `python3 ~/Stocks/_engine/research/brokera.py list` — taken/declined/noted/expired "
        f"are David's ACTUAL calls: engage with a decline's note instead of silently re-recommending, and treat "
        f"an expiry as the default action having stood, not as silence.\n"
        f"TIMING DISCIPLINE (added 2026-07-28 after a false 'executed within 24h' claim):\n"
        f"(a) REAL DATES ONLY: fetch the actual execution ledger with "
        f"`curl -s http://127.0.0.1:8787/api/transactions` (AggregatorA activities; dedupe double-reported "
        f"fills — prefer the row with a nonzero price). When narrating what happened since prior recs, "
        f"reconcile each trade's ACTUAL date/price against the date of the rec that suggested it — trades "
        f"can precede, lag by days, or be unrelated to any rec. NEVER infer timing or causality from "
        f"position-snapshot diffs.\n"
        f"(b) EVENT DATES VERIFIED: any recommendation timed against a catalyst (earnings, readout, "
        f"maturity) must state the event's absolute date and how you verified it (Finnhub earnings "
        f"calendar via _engine keys, company IR page via web, or feed.json earnings). If you cannot "
        f"verify the date, say 'date unverified' and do not build timing logic on it.\n"
        f"(c) VALIDITY WINDOWS: David reads weekly and may execute 0-10 days after you write. Every "
        f"actionable rec must carry: 'Valid until <date or event>' plus a stale-condition ('if executing "
        f"after <date> or with price beyond <level>, do not execute — wait for the next digest'). Use "
        f"absolute dates everywhere; never bare 'now', 'today', or 'in ~N weeks'.\n"
        f"Structure the report exactly as:\n"
        f"1. **Portfolio snapshot** — value, allocation (stocks/cash/money-market/external), each position with "
        f"weight, P&L, and one-line thesis status.\n"
        f"2. **Composition read** — size buckets (micro/small/large), sector mix, valuation profile (P/E or "
        f"fit-for-purpose), concentration, cash drag vs. MMF yield; judge the mix against David's goal of "
        f"long-term compounding and learning the craft, not against a benchmark.\n"
        f"3. **Recommendations** — numbered, concrete, sized in dollars or % of the account; include do-nothing "
        f"when that is right. Cover: capital deployment exactly per the profile's CURRENT capital framing (read "
        f"it fresh — do not carry assumptions over from past digests), position sizing vs. the OABI playbook "
        f"caps, rebalancing, and risk (dilution, binary events, correlation).\n"
        f"4. **Ideas worth research next** — at most 3 names from the board/watchlist/your own screen, each with "
        f"the one-sentence variant perception (per EVALUATION-FRAMEWORK Stage 1) and why the industry's true "
        f"value creation makes sense; mark which single name you'd greenlight for the deep-research engine.\n"
        f"5. **Bear case on my own advice** — what would make each recommendation wrong, and the disconfirming "
        f"evidence to watch.\n"
        f"6. **Scorecard** — review prior digests' recommendations against the ACTUAL ledger "
        f"(curl http://127.0.0.1:8787/api/transactions) and David's decision journal: which recs were taken, "
        f"declined, or pending; how each taken one is aging; one process lesson. If David's notes ask questions "
        f"or share a read, respond to them here by date.\n"
        f"7. **For David** — one thing to learn this week (tied to a live position), and open questions only he "
        f"can answer.\n"
        f"Be specific and honest, argue both sides, no filler, no pitch. If data is unavailable (auth, network), "
        f"say so explicitly in the file rather than inventing numbers."
    )


def launch_rec(now=False):
    if rec_path().exists():
        return {"ok": False, "msg": f"Today's recommendation already exists (rec_{dt.date.today().isoformat()}.md)."}
    if run_state(rec_log()) == "alive":
        return {"ok": False, "msg": "A recommendation session is already running."}
    if not now:
        import claudeq
        return claudeq.enqueue("rec", {}, by="runner")
    return launch(rec_prompt(), rec_log(), kind="rec")      # kind: the digest reads broker quotes


# ---------- daily brief: what happened TODAY to the companies we own ----------
# Deliberately narrower than rec_prompt(): that one re-reads the whole strategy
# weekly; this one answers "what moved my names since the last close, and does
# any of it change anything". The facts are already assembled by the dashboard —
# /api/today is the SAME context the /today page renders, so the brief and the
# screen can never disagree about a number.
BRIEF_DIR = ENGINE / "briefs"


def brief_session_date():
    """The trading date the brief covers — weekends roll back to Friday."""
    d = dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def brief_path(day=None):
    return BRIEF_DIR / f"brief_{day or brief_session_date().isoformat()}.md"


def brief_log():
    return LOGS / "brief.log"


def brief_prompt():
    day = brief_session_date().isoformat()
    return (
        f"Write today's portfolio brief to ~/Stocks/_engine/briefs/brief_{day}.md "
        f"(markdown, start with '# Today — {day}'). Create the briefs/ directory if needed.\n"
        f"WHAT THIS IS: a same-day read on the companies David actually owns — what happened since the "
        f"prior close, what it means, and whether anything needs a decision. It is NOT the weekly strategy "
        f"digest (that's _engine/recommendations/) — do not re-litigate allocation, cash deployment, or new "
        f"ideas here unless today's events force the question.\n"
        f"START WITH THE FACTS, DON'T RE-GATHER THEM: run "
        f"`curl -s http://127.0.0.1:8787/api/today` — that JSON is the exact context the dashboard's /today "
        f"page is showing David right now: every holding across both books with price, day move in percent "
        f"and dollars, value, unrealized P&L, plus each name's news / SEC filings / earnings / fired trigger "
        f"alerts inside the window, and the market backdrop. Use ITS numbers verbatim so the page and the "
        f"brief agree. Only fetch more (EDGAR primary documents, an 8-K exhibit, the earnings release or "
        f"transcript, a press release) where a headline is load-bearing and you need the actual figures — "
        f"and when you do, read the primary source, not the aggregator's summary. EDGAR UA: "
        f"'{EDGAR_UA}'.\n"
        f"THEN READ THE THESIS each event lands on, for the names that actually moved or had news: that "
        f"company's ~/Stocks/*/analysis/FINAL-REPORT.md, the newest analysis/update-*.md, "
        f"analysis/trade-playbook.md if present (sell ladders, add zones, thesis-break triggers), "
        f"analysis/lenses.json, and ~/Stocks/_engine/config/triggers.json price_levels (the notes there are "
        f"David's own pre-committed plan — quote the relevant one when a name is near its level).\n"
        f"THE TWO BOOKS HAVE DIFFERENT RULES — never blur them (see ~/Stocks/CLAUDE.md):\n"
        f"  - BROKERA book (positions.json): David decides every buy/sell. You may recommend, sized and "
        f"specific, but frame it as a recommendation for his approval. Never imply a trade will happen.\n"
        f"  - Agent book (the BrokerB ••••0000 names): autonomous under _engine/agent/MANDATE.md. Do NOT "
        f"place trades from this brief — if something there needs action, say so and note that it belongs in "
        f"a decision session (`loop.py trade`), which writes its own pre-trade memo.\n"
        f"STRUCTURE — keep it tight, one focused read:\n"
        f"1. **The day in three lines** — the combined P&L move, what drove most of it, and whether anything "
        f"today actually matters or it was noise.\n"
        f"2. **What happened, by name** — only names with real events or a move worth explaining. For each: "
        f"what happened (with the number, from the primary source where it matters); whether it confirms, "
        f"strengthens, weakens, or breaks the thesis as written in that name's report; and where the price "
        f"now sits versus that name's pre-committed ladder/levels. Be willing to write 'the stock moved, the "
        f"thesis didn't.'\n"
        f"3. **Explicitly quiet** — one line naming the holdings where nothing happened, so David knows they "
        f"were checked rather than skipped.\n"
        f"4. **Anything to decide** — either concrete and sized ('trim X shares of TK at $Y, per the "
        f"playbook's first rung') with the book named and the BROKERA/agent rule stated, or an honest 'nothing "
        f"today'. Do not manufacture an action. Any level you cite must be one that already exists in the "
        f"playbook or triggers.json, or you must say you are proposing a new one.\n"
        f"5. **Watch next session** — dated, verified catalysts within the next few sessions and the specific "
        f"thing that would change the read.\n"
        f"If a number, date, or filing can't be verified, write 'unverified' — never invent one. Prices are a "
        f"snapshot: state the timestamp from the API payload rather than implying a close."
    )


def launch_brief(force=False, now=False):
    p = brief_path()
    if p.exists() and not force:
        return {"ok": False, "msg": f"Today's brief already exists ({p.name}) — use Rewrite to redo it."}
    lg = brief_log()
    if run_state(lg) == "alive":
        return {"ok": False, "msg": "A brief is already being written."}
    if not now:
        # David asked for it now, so the clock windows do not apply — but it still takes
        # its turn behind whatever Claude session is live (one at a time).
        import claudeq
        r = claudeq.enqueue("brief", {"force": bool(force)}, by="runner", ignore_windows=True)
        if r.get("started"):
            r["msg"] = "Writing today's brief — Claude is reading the day's events against every thesis (a few minutes)."
        return r
    if force and p.exists():
        p.unlink()
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    r = launch(brief_prompt(), lg, kind="brief")            # kind: the brief reads broker quotes
    if r.get("ok"):
        r["msg"] = "Writing today's brief — Claude is reading the day's events against every thesis (a few minutes)."
    return r


def save_token_interactive():
    """Prompt for the `claude setup-token` output and store it in keys.json (0600).
    Run by David in a terminal — the token never passes through chat or logs."""
    import getpass
    tok = getpass.getpass("Paste the token from `claude setup-token` (input hidden): ").strip()
    if not tok.startswith("sk-ant-"):
        sys.exit("That doesn't look like a Claude token (expected it to start with sk-ant-). Nothing saved.")
    kf = CONF / "keys.json"
    keys = json.loads(kf.read_text()) if kf.exists() else {}
    keys["claude_oauth_token"] = tok
    kf.write_text(json.dumps(keys, indent=2))
    os.chmod(kf, 0o600)
    print("Saved to _engine/config/keys.json. Verifying with a live headless call…")
    _ensure_no_mcp()
    r = subprocess.run([CLAUDE_BIN, "-p", "Reply with exactly: AUTH-OK",
                        "--model", job_model(), "--effort", job_effort()] + mcp_args(False),
                       env=clean_env(), capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    if "AUTH-OK" in out:
        print("✓ Headless auth works. Research and daily recommendations are live.")
    else:
        print("✗ Verification failed:", out[-300:])
        sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    reconcile_teardowns()    # cheap, idle once caught up — see runner.py-214
    if args[:1] == ["save-token"]:
        save_token_interactive()
        sys.exit(0)
    now = "--now" in args          # bypass the queue: interactive use only, one at a time still
    if args[:1] == ["rec"]:
        r = launch_rec(now=now)
    elif args[:1] == ["brief"]:
        r = launch_brief(force="--force" in args, now=now)
    elif args[:1] == ["research"] and len(args) > 1:
        r = launch_research(args[1], now=now, full="--full" in args)
    elif args[:1] == ["finish"] and len(args) > 1:
        r = launch_finish(args[1], now=now)
    elif args[:1] == ["update"] and len(args) > 1:
        r = launch_update(args[1], now=now)
    elif args[:1] == ["chain"] and len(args) > 1:
        # N focused updates, one queue job each — the queue serialises them and a limit
        # death re-files the one that died instead of announcing stale verdicts as new
        # (the 2026-09-04 rescore_chain.sh lesson).
        rs = [launch_update(t, now=False) for t in args[1:] if not t.startswith("--")]
        r = {"ok": all(x.get("ok") for x in rs), "jobs": rs,
             "msg": "; ".join(f"{t.upper()}: {x.get('msg', '')}" for t, x in zip([a for a in args[1:] if not a.startswith('--')], rs))}
    elif args[:1] == ["autorefresh"]:
        r = launch_autorefresh()
    else:
        sys.exit("usage: runner.py rec | runner.py brief [--force] | runner.py research TICKER | "
                 "runner.py finish TICKER | runner.py update TICKER | runner.py chain TICKER... | runner.py save-token   "
                 "(--now skips the queue's clock; the slot is still one at a time)")
    print(json.dumps(r))
    sys.exit(0 if r.get("ok") else 1)
