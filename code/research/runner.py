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
    # four hours of work. 0 = wait for them.
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    tok = saved_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def job_model(role=None):
    """Model for a headless session. David's standing instruction (2026-08-04): research
    and recommendation sessions are always Opus, tracking the most recent release — the
    'opus' alias resolves to the latest.

    With a `role`, the ORG CHART decides (agent/roster.py `model` field), because a role's
    cost/capability belongs next to its purpose and charter rather than in a --model flag
    at each call site. That is how the fixer stayed on Opus for weeks after its work had
    become mechanical. keys.json `claude_model` still overrides everything, globally."""
    try:
        override = json.loads((CONF / "keys.json").read_text()).get("claude_model")
    except Exception:
        override = None
    if override:
        return override
    if role:
        try:
            import sys as _s
            _s.path.insert(0, str(ENGINE / "agent"))
            import roster
            return roster.claude_model(role)
        except Exception:
            pass
    return "opus"


_CHILDREN = {}   # pid -> Popen, so this process reaps what it spawned


def pid_file(log_path):
    return Path(str(log_path) + ".pid")


def launch(prompt, log_path):
    ok, msg = auth_check()
    if not ok:
        return {"ok": False, "msg": msg}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w")
    try:
        p = subprocess.Popen(["claude", "-p", prompt, "--dangerously-skip-permissions",
                              "--model", job_model()],
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
    return {"ok": True, "pid": p.pid}


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
        f"offsets, INDEX.md navigation map with local-model reading notes). Create any missing "
        f"~/Stocks/<CompanyName>-{tk}/ dirs (filings/ financials/ research/ analysis/, Name-TICKER convention "
        f"like OmniAB-OABI). EVERY research agent you spawn must be told to read INDEX.md + facts.json FIRST "
        f"and cite pack files where they suffice — pulling additional raw EDGAR material stays allowed and "
        f"expected whenever the pack is thin (the pack narrows search, it never limits it); (1) fan out parallel research agents in one "
        f"message — governance/insiders/ownership, product/pipeline/business, competitive/market, IP/legal/moat, "
        f"financials/capital-structure — each writing a sourced research/<topic>.md; (2) reconcile, hunt the "
        f"load-bearing contradiction, verify value actually accrues to shareholders, and build the explicit bear "
        f"case; (2.5) REQUIRED, per RUNBOOK §2.5 — fire ONE fresh context-isolated adversarial-refuter agent that "
        f"reads ONLY research/*.md and the primary record (filings/, EDGAR, XBRL, transcripts — NEVER analysis/), "
        f"attempts to overturn each load-bearing claim with a primary-source citation, and writes "
        f"research/adversarial-review.md with per-claim overturned/hardened/unchanged verdicts (unsourced doubt = "
        f"unchanged; hardened must carry the strongest counter-evidence found); the FINAL-REPORT must cite how "
        f"each pillar survived the refuter and lenses.json overall.summary must end with "
        f"'(N overturned / M hardened of K claims — see adversarial-review.md)', and after writing it update the "
        f"adversarial-verifier-research row in ~/memos/LEDGER.md from implemented to verified with the counts "
        f"(first-run evidence, per the ledger's 2026-08-10 amendment); "
        f"(3) synthesize analysis/soft-research-dossier.md, a fit-for-purpose valuation (DCF+reverse-DCF if "
        f"cash-generative, rNPV/sum-of-parts if optionality — never force the wrong one, always add comps) with "
        f"bear/base/bull per share vs. price, analysis/trade-playbook.md (sizing, tranches, sell ladder, "
        f"thesis-break triggers), analysis/FINAL-REPORT.md, and analysis/lenses.json using the same schema as "
        f"~/Stocks/OmniAB-OABI/analysis/lenses.json so the dashboard renders it, plus analysis/card.json "
        f"(the one-page thesis card, same schema as OmniAB-OABI/analysis/card.json — thesis sentence, state, "
        f"now/later actions, milestones, ladder, kill triggers; 2-4 items per list). Be honest, argue both "
        f"sides, and report gaps plainly — this informs David's decision, it is not a pitch."
    )


def launch_research(tk):
    return launch(research_prompt(tk), research_log(tk))


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
        f"and stale-condition (absolute dates);\n"
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
        f"Also: update analysis/lenses.json (same schema) if the read changed; prepend a line "
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


def launch_update(tk):
    tk = tk.upper()
    cd = company_dir(tk)
    if not cd:
        return {"ok": False, "msg": f"No research folder for {tk} — run a full Deep research instead."}
    today_f = f"update-{dt.date.today().isoformat()}.md"
    if (cd / "analysis" / "updates" / today_f).exists() or (cd / "analysis" / today_f).exists():
        return {"ok": False, "msg": f"{tk} already has an update dated today."}
    lg = update_log(tk)
    if lg.exists() and time.time() - lg.stat().st_mtime < 900:
        return {"ok": False, "msg": f"An update for {tk} looks in-flight (log active <15 min ago)."}
    rep = cd / "analysis" / "FINAL-REPORT.md"
    rdate = dt.date.fromtimestamp(rep.stat().st_mtime).isoformat() if rep.exists() else "unknown"
    prompt = update_prompt(tk, cd.name, rdate)
    topic = _ntfy_topic()
    if tk in _held() and topic:
        prompt += (f"\nFINALLY — {tk} is a HELD position, so notify David that the refresh landed: "
                   f"curl -s -H 'Title: Research updated: {tk}' "
                   f"-d '<one line: your section-4 action, e.g. HOLD — thesis intact, next catalyst <date>>' "
                   f"https://ntfy.sh/{topic}")
    return launch(prompt, update_log(tk))


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


def launch_rec():
    if rec_path().exists():
        return {"ok": False, "msg": f"Today's recommendation already exists (rec_{dt.date.today().isoformat()}.md)."}
    return launch(rec_prompt(), rec_log())


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


def launch_brief(force=False):
    p = brief_path()
    if p.exists() and not force:
        return {"ok": False, "msg": f"Today's brief already exists ({p.name}) — use Rewrite to redo it."}
    if force and p.exists():
        p.unlink()
    lg = brief_log()
    if lg.exists() and time.time() - lg.stat().st_mtime < 300 and not log_error(lg):
        return {"ok": False, "msg": "A brief looks in-flight (log active <5 min ago)."}
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    r = launch(brief_prompt(), lg)
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
    r = subprocess.run(["claude", "-p", "Reply with exactly: AUTH-OK"],
                       env=clean_env(), capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    if "AUTH-OK" in out:
        print("✓ Headless auth works. Research and daily recommendations are live.")
    else:
        print("✗ Verification failed:", out[-300:])
        sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["save-token"]:
        save_token_interactive()
        sys.exit(0)
    if args[:1] == ["rec"]:
        r = launch_rec()
    elif args[:1] == ["brief"]:
        r = launch_brief(force="--force" in args)
    elif args[:1] == ["research"] and len(args) > 1:
        r = launch_research(args[1])
    elif args[:1] == ["update"] and len(args) > 1:
        r = launch_update(args[1])
    elif args[:1] == ["autorefresh"]:
        r = launch_autorefresh()
    else:
        sys.exit("usage: runner.py rec | runner.py brief [--force] | runner.py research TICKER | "
                 "runner.py update TICKER | runner.py save-token")
    print(json.dumps(r))
    sys.exit(0 if r.get("ok") else 1)
