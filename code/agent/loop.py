#!/usr/bin/env python3
"""
BrokerB agentic-account loop — headless Claude sessions against the
brokerb-trading MCP (project-scope for ~/Stocks since 2026-08-19; the loop itself
uses --strict-mcp-config with _engine/config/agent_mcp.json, OAuth already established).

Modes
  sync   read-only: pull portfolio/positions/orders/recent activity from the MCP
         and write data/portfolio.json + data/trades.json for the dashboard.
         Never places orders. Cheap; runs daily after close via cron.
  trade  a full decision session under MANDATE.md: read feed.json + portfolio +
         journal, decide, write a pre-trade memo per order BEFORE placing it,
         place via review->place, then update the ledger and sync files.
         Enabled by cron only once the account is funded.

Auth: reuses research/runner.py (saved subscription token + clean env).
Every session's transcript tail lands in _engine/logs/agent_<mode>.log.
CLI: loop.py sync | loop.py trade
"""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
ROOT = ENGINE.parent
sys.path.insert(0, str(ENGINE / "research"))
import runner  # noqa: E402  (clean_env, auth_check)

DATA = HERE / "data"
JOURNAL = HERE / "journal"
LOGS = ENGINE / "logs"
# The PM sees ONLY the broker (2026-08-12): user-scope Gmail/Calendar/Drive MCPs are
# irrelevant context + surface for a trading session. UNVERIFIED until the supervised
# 2026-08-13 session confirms OAuth reuse under --strict-mcp-config; if brokerb tools
# come up missing, drop MCP_RESTRICT from the command to fall back to full user scope.
MCP_RESTRICT = ["--strict-mcp-config", "--mcp-config", str(ENGINE / "config" / "agent_mcp.json")]

SYNC_PROMPT = f"""READ-ONLY sync of the BrokerB AGENTIC account (the account with agentic=Yes; never any other).
Do NOT place, modify, or cancel any orders. Using the brokerb-trading MCP tools:
1) get_accounts + get_portfolio for the agentic account -> write {DATA}/portfolio.json as:
   {{"as_of": "<UTC ISO>", "account": "<masked number>", "total_value": N, "cash": N,
     "buying_power": N, "positions": [{{"symbol","qty","avg_cost","price","value","pnl","pnl_pct"}}],
     "pending_deposits": N}}
2) get_equity_orders (or the equivalent order/activity tools) -> append any orders/fills not already
   present (match by order id) to {DATA}/trades.json, a JSON list of
   {{"ts","symbol","side","qty","price","status","order_id","memo": "<journal/<file>.md if one exists, else null>"}}.
   Create the file as [] if missing. Preserve existing entries and their memo links.
3) Print a one-line summary. Write the files with the Write tool. Nothing else."""

# The PM's instructions live in prompts/pm.md (decision-core, David-owned) and are
# rendered by prompts.py at launch — see prompts.py for why they are a file and not a
# string here. A placeholder that does not resolve fails the launch loudly.
def trade_prompt():
    """The PM's instructions plus its reflections (REFLECTION.md): the lessons its own
    evaluators and it wrote after the last session. Reflection rendering fails OPEN with a
    visible marker — a session without its lessons is worse than one with them, but a
    trading session that cannot start because a ledger is unreadable is worse still."""
    import prompts
    try:
        import reflect
        block = reflect.render("frontier", record=True)
    except Exception as e:
        block = (f"_reflect.py could not render your reflections ({type(e).__name__}: {e}). You are "
                 "running WITHOUT last session's lessons — say so in your session log and open an "
                 "ask against _engine/agent/reflect.py._")
    return prompts.render("pm", REFLECTIONS=block)


def launch(mode, attempt=1, model_idx=0):
    # sync is a mechanical JSON fetch — CODE does it now (mcp_sync.py, 2026-08-13,
    # after David's notification archaeology found Claude sessions doing curl work).
    # A Claude session remains the FALLBACK so token expiry never leaves a gap.
    if mode == "sync":
        try:
            import mcp_sync
            mcp_sync.sync()
            return {"ok": True, "msg": "code-sync complete (no model)"}
        except Exception as e:
            print(f"code-sync failed ({str(e)[:100]}) — falling back to claude session")
    if mode == "trade":
        # ONE PM AT A TIME (ask triggers.py-099, 2026-09-02): the KPI-breach ACTION launched a
        # second PM session at 14:15Z while the 14:05Z one was alive — two writers on
        # BOOK.md / trades.json / thesis.json. A live pid refuses the launch; the trigger's
        # alert still fires and the live session sees it in its feed.
        pidf = DATA / "trade_session.pid"
        try:
            old_pid = int(pidf.read_text().strip())
            import os as _os
            _os.kill(old_pid, 0)
            with open(f"/proc/{old_pid}/cmdline", "rb") as fh:
                if b"claude" in fh.read():
                    return {"ok": False, "msg": f"trade session already live (pid {old_pid}) — not launching a second PM"}
        except Exception:
            pass
    ok, msg = runner.auth_check()
    if not ok:
        return {"ok": False, "msg": msg}
    DATA.mkdir(exist_ok=True)
    JOURNAL.mkdir(exist_ok=True)
    (JOURNAL / "sessions").mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    try:
        prompt = SYNC_PROMPT if mode == "sync" else trade_prompt()
    except Exception as e:   # a prompt with a hole is not a session; say so, do not launch
        return {"ok": False, "msg": f"prompt did not render: {type(e).__name__}: {e}"}
    if mode == "trade":
        # coded pre-work: "what changed since last session" brief (fast, no tokens)
        try:
            subprocess.run(["python3", str(HERE / "diffbrief.py")], timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # the packet (desk.py): one file, fresh — the inbox, alerts and delta as of this minute
        try:
            subprocess.run(["python3", str(HERE / "desk.py"), "build"], timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    log = open(LOGS / f"agent_{mode}.log", "w")
    # trade sessions think on Opus (David, 2026-08-04); syncs are mechanical — default model
    cmd = [runner.CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"] + MCP_RESTRICT
    models = runner.job_models("pm")
    if mode == "trade":
        # the org chart's tier for the PM (roster CLAUDE_TIERS["best"]); _launch_guard falls
        # back to the next entry if the installed CLI cannot run this one
        cmd += ["--model", models[min(model_idx, len(models) - 1)]]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=log,
                            start_new_session=True, env=runner.clean_env())
    if mode == "trade":
        try:
            (DATA / "trade_session.pid").write_text(str(proc.pid))
        except Exception:
            pass
        # order-lifecycle experiment (memo 2026-08-08): after the session exits, a detached
        # watcher runs `loop.py reconcile` — real broker ledger vs. the agent's claimed states.
        # Fast broker sync FOR AS LONG AS THE PM IS LIVE (David, 2026-08-18: "when PM is
        # running should be syncing realtime/fast as possible... when PM session done can
        # go back to normal"). The PM's own pid is the switch, so there is no flag to
        # forget to unset and a crashed session cannot leave fast-sync running.
        subprocess.Popen(["python3", str(HERE / "mcp_sync.py"), "follow", str(proc.pid)],
                         cwd=str(HERE), start_new_session=True,
                         stdout=open(LOGS / "agent_sync_follow.log", "a"),
                         stderr=subprocess.STDOUT)
        subprocess.Popen(["bash", "-c",
                          f"while kill -0 {proc.pid} 2>/dev/null; do sleep 20; done; "
                          f"python3 {HERE}/loop.py reconcile >> {LOGS}/agent_reconcile.log 2>&1"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if mode == "trade":
        died = _launch_guard(proc, attempt, model_idx, models)
        if died:
            return died
    return {"ok": True, "msg": f"agent {mode} launched on {models[min(model_idx, len(models) - 1)]}"}


def _launch_guard(proc, attempt, model_idx=0, models=("opus",)):
    """THE TRADE SESSION NEVER DIES QUIETLY (David, 2026-08-31: "the trade session
    should never die"). That day's 14:05 launch lasted 1 second — "You've hit your
    session limit · resets 2:40pm (UTC)" — and the desk stayed dark until a human
    noticed at 14:54. Watch the first 90s of the session: a usage-limit death gets a
    critical page AND one detached relaunch just after the stated reset (≤3 attempts,
    never after 19:30 UTC — a session that opens in the last half hour of the market
    day can't do its job). A death that isn't the limit belongs to auth_check and the
    reconcile watcher, not this guard."""
    import re
    import time as _t
    for _ in range(18):
        _t.sleep(5)
        if proc.poll() is not None:
            break
    if proc.poll() is None:
        return None
    try:
        tail = (LOGS / "agent_trade.log").read_text()[-600:]
    except Exception:
        tail = ""
    # (0) the model itself: "Claude Code X does not support this model" — the tier named a
    # model the installed CLI cannot run. Relaunch NOW on the next entry; never let a
    # version skew kill the trade session (2026-09-01: 2.1.236 refused claude-fable-5-1).
    if "does not support this model" in tail.lower() or "invalid model" in tail.lower():
        nxt = model_idx + 1
        if nxt < len(models):
            try:
                sys.path.insert(0, str(ENGINE))
                import notify as _n
                _n.push("Agent trade session: model fallback",
                        f"{models[model_idx]} refused by the CLI ({tail.strip()[-100:]}); relaunching on "
                        f"{models[nxt]}. Run `claude install latest` on the box.", tier="critical")
            except Exception:
                pass
            return launch("trade", attempt=attempt, model_idx=nxt)
        return {"ok": False, "msg": f"trade died: no model in {list(models)} runs on this CLI — needs eyes"}
    if "limit" not in tail.lower():
        return None
    now = dt.datetime.now(dt.timezone.utc)
    delay = 3600
    m = re.search(r"resets (\d{1,2}):(\d{2})\s*(am|pm)\s*\(UTC\)", tail, re.I)
    if m:
        hh = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        reset = now.replace(hour=hh, minute=int(m.group(2)), second=0, microsecond=0)
        if reset <= now:
            reset += dt.timedelta(days=1)
        delay = int((reset - now).total_seconds()) + 300
    launch_at = now + dt.timedelta(seconds=delay)
    retry = attempt < 3 and (launch_at.hour, launch_at.minute) < (19, 30) \
        and launch_at.date() == now.date()
    try:
        sys.path.insert(0, str(ENGINE))
        import notify as _n
        _n.push("Agent trade session FAILED on usage limit",
                f"attempt {attempt} died at launch ({tail.strip()[-120:]}). "
                + (f"Auto-retry at {launch_at:%H:%M}Z." if retry
                   else "NO retry (attempts exhausted or too late in the day) — needs eyes."),
                tier="critical")
    except Exception:
        pass
    if retry:
        subprocess.Popen(["bash", "-c",
                          f"sleep {delay}; cd {HERE}; python3 loop.py trade {attempt + 1} {model_idx} "
                          f">> {LOGS}/agent_cron.log 2>&1"],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": False, "msg": f"trade died on usage limit; retry {attempt + 1} at {launch_at:%H:%M}Z"}
    return {"ok": False, "msg": "trade died on usage limit; no retry scheduled — needs eyes"}


def reconcile():
    """Post-session order reconciliation (state-machine experiment, memo 2026-08-08).
    Runs a blocking read-only sync (Claude pulls the REAL broker ledger via MCP into
    trades.json, merging by order_id with source:mcp), then diffs: any agent-claimed
    placed/filled row the broker doesn't confirm -> 'unresolved' + ntfy. Catches the
    trades-executed-hallucination class; does NOT judge real-but-wrong trades."""
    import datetime as dtm
    import time as _t
    def _cleared(r):
        # a 'reconciled' stamp clears only the state it was stamped AT (loop.py-039,
        # authorised 2026-08-23): a row stamped at 'proposed' carried nothing the
        # evidence checks apply to, and must face the gate again once it moves on
        hist = r.get("state_history") or []
        last = {h.get("state"): i for i, h in enumerate(hist)}
        return last.get("reconciled", -1) > last.get(r.get("state"), len(hist))
    before = {r.get("order_id"): r for r in _load_trades() if r.get("order_id")}
    claimed = [r for r in _load_trades()
               if (r.get("state") in ("placed", "filled", "partial"))
               and any(h.get("source") == "agent" for h in (r.get("state_history") or []))
               and not _cleared(r)]
    # blocking sync: broker truth into trades.json/portfolio.json — code first
    # (mcp_sync), claude session only as fallback (2026-08-13: this inner claude
    # sync was a shadow session after EVERY trade session — David saw the pile-up)
    try:
        import mcp_sync
        mcp_sync.sync()
    except Exception as e:
        print(f"code-sync failed in reconcile ({str(e)[:100]}) — claude fallback")
        subprocess.run([runner.CLAUDE_BIN, "-p", SYNC_PROMPT, "--dangerously-skip-permissions"] + MCP_RESTRICT,
                       cwd=str(ROOT), env=runner.clean_env(), timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = _load_trades()
    now = dtm.datetime.now(dtm.timezone.utc).isoformat(timespec="seconds")
    problems = []
    for r in rows:
        hist = r.get("state_history") or []
        agent_claimed = any(h.get("source") == "agent" for h in hist)
        if not agent_claimed or any(h.get("state") == "unresolved" for h in hist) or _cleared(r):
            continue
        issues = []
        if r.get("state") in ("placed", "filled", "partial") and not r.get("order_id"):
            issues.append("no order_id from broker")
        if r.get("state") in ("placed", "filled", "partial") and r.get("spread_pct") is None:
            issues.append("no spread_pct recorded at preview (ACT contract, 2026-08-12)")
        memo = r.get("memo")
        if memo and not (ROOT / memo).exists() and not (JOURNAL / Path(memo).name).exists():
            issues.append(f"memo file missing: {memo}")
        elif memo:
            # evidence gate (2026-08-12): every placed order's memo must carry a clean
            # claim audit — an unaudited memo is flagged like a fabricated order state
            mp = (ROOT / memo) if (ROOT / memo).exists() else (JOURNAL / Path(memo).name)
            ap = mp.with_suffix(".audit.json")
            if not ap.exists():
                issues.append("memo claims never audited (dossier.py audit)")
            else:
                try:
                    rep = json.loads(ap.read_text())
                    bad = [x for x in rep.get("claims", []) if x.get("verdict") == "CONTRADICTED"]
                    if bad:
                        issues.append(f"memo audit: {len(bad)} CONTRADICTED claim(s)")
                    elif rep.get("n_unresolved"):
                        issues.append(f"memo audit: {rep['n_unresolved']} unresolved claim(s)")
                except Exception:
                    issues.append("memo audit unreadable")
        # broker confirmation: sync merges by order_id; a confirmed row gains broker fields
        if r.get("state") == "filled" and r.get("order_id") and not (r.get("filled_at") or r.get("avg_fill_price")):
            issues.append("claimed filled; broker shows no fill")
        if issues:
            r["state"] = "unresolved"
            hist.append({"state": "unresolved", "ts": now, "source": "mcp", "why": "; ".join(issues)})
            problems.append(f"{r.get('symbol')} {r.get('side')}: {'; '.join(issues)}")
        else:
            hist.append({"state": "reconciled", "ts": now, "source": "mcp"})
        r["state_history"] = hist
    # atomic temp+os.replace — same writer as every other trades.json producer (loop.py-039);
    # keep the same shape even when mcp_sync is unavailable (the claude-fallback world)
    import os as _os
    try:
        from mcp_sync import _write_json as _atomic
    except (ImportError, AttributeError):
        def _atomic(p, obj, indent=1):
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, indent=indent))
            _os.replace(tmp, p)
    _atomic(DATA / "trades.json", rows)
    if problems:
        try:
            import notify as _n
            _n.push("Stocks · agent UNRESOLVED orders",
                    "Agent order reconciliation FAILED:\n" + "\n".join(problems))
        except Exception:
            pass
    # memo-ledger self-verification (amendment 2026-08-10): first instrumented run
    # promotes the state-machine row from implemented -> verified with evidence.
    try:
        led = Path.home() / "memos" / "LEDGER.md"
        txt = led.read_text()
        if "order-lifecycle-state-machine" in txt and "verified 20" not in txt.split("order-lifecycle-state-machine")[1].split("\n")[0]:
            old_row = [l for l in txt.splitlines() if "order-lifecycle-state-machine" in l][0]
            ev = f"**verified {dtm.date.today().isoformat()}**: reconcile ran post-session — {len(claimed)} agent-claimed rows checked, {len(problems)} unresolved"
            led.write_text(txt.replace(old_row, old_row.rstrip(" |") + f"; {ev} |"))
    except Exception:
        pass
    # desk-consistency sweep (2026-08-12): every producer->consumer promise, checked
    try:
        import contract
        cv = contract.check()
        if cv:
            try:
                import notify as _n
                _n.push("Stocks · agent CONTRACT",
                        "Agent desk contract violations:\n" + "\n".join(cv[:10]),
                        tier="actionable")  # money-book invariants; default-tiered digest, held (08-31)
            except Exception:
                pass
        print(f"{now} contract: {len(cv)} violation(s)" + (" — " + "; ".join(cv[:4]) if cv else ""))
    except Exception as e:
        print(f"{now} contract check failed: {e}")
    # the reflection ledger (REFLECTION.md): evaluators -> findings, the PM's own
    # `## Reflection` -> lessons, strikes/absorb. Once per session file; never fatal.
    try:
        import reflect
        rr = reflect.after_session(reconcile_problems=problems)
        print(f"{now} reflect: {rr.get('session')} — {len(rr.get('new_findings', []))} new finding(s), "
              f"{rr.get('lessons_ingested', 0)} lesson(s) ingested, strikes {rr.get('strikes', [])}, "
              f"absorbed {rr.get('absorbed', [])}, refused {rr.get('refused', [])}, "
              f"incidents {rr.get('incidents', [])}")
    except Exception as e:
        print(f"{now} reflect failed: {type(e).__name__}: {e}")
    # the learning loop (learn.py): anchors -> predictions, memos' ## Predictions, resolve what
    # is due, rebuild the Friday brief. Then how the PM spent its session (pmusage.py). Never fatal.
    try:
        import learn
        learn.sync_anchors()
        ing = learn.ingest()
        due = learn.resolve_due()
        learn.brief()
        print(f"{now} learn: +{len(ing.get('added', []))} prediction(s) from memos, {len(due)} resolved, "
              f"refused {ing.get('refused', [])}")
    except Exception as e:
        print(f"{now} learn failed: {type(e).__name__}: {e}")
    try:
        import pmusage
        u = pmusage.record()
        print(f"{now} pmusage: " + (f"{u.get('duration_min')} min, {u['tokens']['output']:,} tokens written, "
                                     f"{u['by_category']['research']['share_pct']}% research / "
                                     f"{u['by_category']['reports']['share_pct']}% reports / "
                                     f"{u['by_category']['deciding']['share_pct']}% deciding" if u.get("ok") else u.get("msg")))
    except Exception as e:
        print(f"{now} pmusage failed: {type(e).__name__}: {e}")
    # David's daily read (pmreport.py): the session's decisions as a table + how the machine
    # is running. Code-written, after the ledger has ingested the session. Never fatal.
    try:
        import pmreport
        print(f"{now} pmreport: wrote {pmreport.write()}")
    except Exception as e:
        print(f"{now} pmreport failed: {type(e).__name__}: {e}")
    print(f"{now} reconcile: {len(claimed)} agent-claimed rows checked, {len(problems)} unresolved")
    return {"ok": True, "unresolved": problems}


def _load_trades():
    try:
        return json.loads((DATA / "trades.json").read_text())
    except Exception:
        return []


def status(mode):
    """For the dashboard: is a run in flight / when did data last update?"""
    log = LOGS / f"agent_{mode}.log"
    pf = DATA / "portfolio.json"
    return {
        "log_mtime": log.stat().st_mtime if log.exists() else None,
        "portfolio_mtime": pf.stat().st_mtime if pf.exists() else None,
    }


if __name__ == "__main__":
    m = sys.argv[1] if sys.argv[1:] else ""
    if m == "reconcile":
        r = reconcile()
    elif m in ("sync", "trade"):
        att = int(sys.argv[2]) if sys.argv[2:] and sys.argv[2].isdigit() else 1
        midx = int(sys.argv[3]) if sys.argv[3:] and sys.argv[3].isdigit() else 0
        r = launch(m, attempt=att, model_idx=midx)
    else:
        sys.exit("usage: loop.py sync | loop.py trade | loop.py reconcile")
    print(json.dumps(r))
    sys.exit(0 if r["ok"] else 1)
