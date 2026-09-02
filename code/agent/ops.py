#!/usr/bin/env python3
"""
Ops sessions — the agent's back office (David, 2026-08-13; org built out 2026-08-19
per ORG_PLAN.md).

Four headless Claude roles, NONE of which can touch the broker (empty MCP set):

  numbers  (alias: fixer) 07:05 UTC Tue-Sat, Sonnet: the accounting-forensics
           engineer. Works the quality queue + its ask inbox to zero; owns the
           number pipeline per owners.py; every fix proven by a re-scan.
  signals  07:35 UTC Tue-Sat, Sonnet: the signal-vs-noise engineer. Owns the
           origination/intel pipeline (scout, feeds, relevance, cannibal, bench,
           trigger RULES); its job is proving each feed is actually saying
           something rather than running silently.
  coo      Saturday 15:00 UTC, Opus: process review. Clears the week's asks,
           verifies a sample of the week's CLOSES actually hold (trust but
           verify), spot-checks numbers from primary sources, reads logs for
           silent failures.
  hunt     08:30 UTC Thu + Sat, Opus: adversarial defect search. Owns nothing,
           deliberately; receives every two-strikes ask; rotates six areas
           including `seams` — what falls between the owners.

Reports land in journal/ops/ (gitignored). Roles may commit+push code fixes
within their owners.py charter — NEVER trading logic (loop.py, MANDATE.md,
triggers' launch/ACTION-cap logic, thesis.json, journal memos).
CLI: ops.py numbers|signals|coo|hunt   (fixer accepted as alias for numbers)

NOTE 2026-08-19: this CLI's gate used to read `if r in ("fixer", "coo")` while
the crontab had been calling `ops.py hunt` since 2026-08-18 — the hunt NEVER
LAUNCHED and nothing noticed, which is precisely the class of failure the
ownership map exists to end. The gate now derives from the same dict as the
prompts, so a role cannot exist half-way.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
ROOT = ENGINE.parent
LOGS = ENGINE / "logs"
OPS = HERE / "journal" / "ops"
sys.path.insert(0, str(ENGINE / "research"))
import runner  # noqa: E402

NO_MCP = ENGINE / "config" / "ops_mcp.json"
HUNT_LEDGER = HERE / "journal" / "ops" / "hunt_coverage.json"

# The bug hunt's rotation, and the ONLY place the area list is written down. The prompt's
# table is rendered from this dict and the coverage ledger is upserted from it on every
# launch (_hunt_ledger_sync), so an area cannot exist in the charter and be unreachable in
# the ledger — which is exactly what happened to `seams`: the ledger was hand-seeded with
# five areas on 2026-08-18 while the prompt described six, and selection reads the LEDGER
# (ask ops.py-010, opened by the PM after fixing that instance by hand).
HUNT_AREAS = {
    "number-pipeline": "valuation/fincard.py · query.py · xbrlfacts.py · refresh_cards.py",
    "evidence-reading": "dossier.py · numwatch.py · bench.py · research/predigest.py · navindex.py",
    "decision-path": "loop.py · triggers.py · contract.py · thesis.json/trades.json state",
    "orchestration": "vp.py · ops.py · roster.py · unknowns.py · asof.py · sweepcheck.py · scout.py",
    "surfaces": "dashboard/agent_page.py · app.py · the /agent page and every link on it",
    "seams": "what falls BETWEEN owners (owners.py is the map): the handoffs\n"
             "                    (feeds->relevance->scout, fincard->dossier->numwatch, engineer reports->PM\n"
             "                    desk), anything `owners.py check` says is unowned, launch paths (does every\n"
             "                    role in the crontab actually start? this CLI rejected `hunt` for days), and\n"
             "                    this week's CORRECTLY-handled asks (`asks.py list` + traces) where the\n"
             "                    boundary was the real problem. No owner will ever report these, because\n"
             "                    none of them is theirs.",
}


def _hunt_area_table():
    """The prompt's area table, rendered from HUNT_AREAS so prompt and ledger cannot drift."""
    return "\n".join(f"  {name:<17} {desc}" for name, desc in HUNT_AREAS.items())


def _hunt_ledger_sync():
    """Upsert every HUNT_AREAS key into the coverage ledger BEFORE the hunt selects a target.
    A new area lands as never-hunted (last_hunt None), so the 'oldest last_hunt or any never
    hunted' rule picks it up on the very next run instead of never. Never removes an area:
    a retired one keeps its history and simply stops being rendered in the prompt."""
    try:
        led = json.loads(HUNT_LEDGER.read_text()) if HUNT_LEDGER.exists() else {}
    except Exception:
        led = {}
    led.setdefault("_doc", "bug-hunt coverage — each area's last hunt and what was found. "
                           "Areas are UPSERTED from ops.HUNT_AREAS on every launch (ops.py-010).")
    areas = led.setdefault("areas", {})
    added = [a for a in HUNT_AREAS if a not in areas]
    for a in added:
        areas[a] = {"last_hunt": None, "found": 0,
                    "note": "registered automatically from ops.HUNT_AREAS; never hunted"}
    if added:
        HUNT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = HUNT_LEDGER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(led, indent=1))
        os.replace(tmp, HUNT_LEDGER)
    return added
DATA = HERE / "data"

# The CLI gate, the prompt lookup, and the model lookup all derive from THIS dict — a role
# cannot exist in one and not the others. Its absence is how `ops.py hunt` sat in the crontab
# from 2026-08-18 being rejected by a gate that read `("fixer", "coo")`, and no hunt ever ran.
# Each role's instructions are a FILE — prompts/<role>.md, rendered by prompts.py — so the
# PM can change a job description without touching this launcher (owners.py: the prompts
# tree belongs to the PM; this file stays with the COO). The hunt's two live inputs are
# supplied here because only the launcher knows them.
import prompts

PROMPTS = {"numbers": lambda: prompts.render("numbers"),
           "signals": lambda: prompts.render("signals"),
           "build": lambda: prompts.render("build"),
           "coo": lambda: prompts.render("coo"),
           "hunt": lambda: prompts.render("hunt", HUNT_LEDGER=str(HUNT_LEDGER),
                                          HUNT_AREAS=_hunt_area_table())}
ROLE_ALIASES = {"fixer": "numbers"}

# Appended to EVERY role prompt (ask ops.py-025): prompts/_session_mechanics.md — the
# single-turn finish contract. On 2026-08-20 the Numbers Engineer rebuilt 150 cards,
# then ended its turn to "genuinely wait" for background notifications — and died.
def single_turn_note():
    return "\n\n" + prompts.render("_session_mechanics")


# role -> (report globs, cron hour, cron minute, weekdays as Mon=0) — the finish contract
# `ops.py verify` enforces. Same dict philosophy as PROMPTS: a role cannot be launchable
# without being verifiable. A role may carry MORE THAN ONE glob: the fixer was renamed to
# "numbers" on 2026-08-18 and wrote its first *_numbers.md on 2026-08-22, which verify()
# then reported as a missed finish because it only knew the old name (ask ops.py-042). A
# rename must not read as a dead role, so legacy names stay listed.
#
# Since 2026-09-01 the SCHEDULE half comes from the crontab itself (roster.cron_schedule —
# the `cron` matcher on the org chart), so moving a job in `crontab -e` moves its finish
# check and the dashboard's "Did they run?" card with it. The hand-typed times below are
# the fallback for a box whose crontab cannot be read, and they are what the crontab said
# on 2026-09-01.
_FINISH_GLOBS = {"numbers": ("*_numbers.md", "*_fixer.md"), "signals": ("*_signals.md",),
                 "hunt": ("*_hunt.md",), "coo": ("*_coo.md",), "build": ("*_build.md",)}
_FINISH_FALLBACK = {"numbers": (7, 5, {1, 2, 3, 4, 5}), "signals": (7, 35, {1, 2, 3, 4, 5}),
                    "hunt": (8, 30, {3, 5}), "coo": (15, 0, {5}), "build": (8, 30, {4})}


def _finish():
    out = {}
    try:
        sys.path.insert(0, str(HERE))
        import roster
        by_id = {r["id"]: r for r in roster.ROLES}
    except Exception:
        by_id = {}
    for role, globs in _FINISH_GLOBS.items():
        sched = []
        r = by_id.get(role)
        if r is not None and r.get("cron"):
            try:
                sched = roster.cron_schedule(r["cron"])
            except Exception:
                sched = []
        if sched:
            # one launch a day per role; several fixed times would be several finishes,
            # which no role has — take the first and let roster.cron_drift say if that changes
            h, m, days = sched[0]
        else:
            h, m, days = _FINISH_FALLBACK[role]
        out[role] = (globs, h, m, days)
    return out


FINISH = _finish()


def _last_scheduled(hour, minute, weekdays, slack_h=3):
    import datetime as dt
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=slack_h)
    for back in range(14):
        d = t - dt.timedelta(days=back)
        cand = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if d.weekday() in weekdays and cand <= t:
            return cand
    return None


def verify():
    """Did every scheduled role FINISH — i.e. leave its dated report? A missing one gets a
    phone alert and an ask to the COO (the asks channel, not a bespoke ledger, so C15 ages
    it and the board shows it). Run from cron daily, after the morning windows."""
    misses = []
    for role, (pats, h, m, days) in FINISH.items():
        due = _last_scheduled(h, m, days)
        if due is None:
            continue
        newest = max((p.stat().st_mtime for pat in pats for p in OPS.glob(pat)), default=0)
        if newest < due.timestamp():
            misses.append(f"{role}: scheduled {due:%Y-%m-%d %H:%M}Z, newest report "
                          f"{'NONE' if not newest else 'older than that run'}")
    # ops.py-053 (structural): a missed role gets ONE same-day relaunch before any human
    # is alerted — the 8/25 numbers death sat as a COO ask for four days because the COO
    # runs Saturdays. Marker file per role per day guards against relaunch loops; if the
    # NEXT verify still finds the miss (marker present), it escalates as before.
    import datetime as _dt
    healed = []
    for msg in list(misses):
        role = msg.split(":")[0]
        marker = DATA / f"verify_relaunch_{role}_{_dt.date.today().isoformat()}"
        if role in PROMPTS and not marker.exists():
            marker.write_text(msg)
            # USAGE-WINDOW RULE (David 2026-08-31 / 2026-09-01): no Claude session starts inside
            # 09:05-14:05 UTC Mon-Fri, the 14:05 trade session's 5-hour lookback. verify runs at
            # 11:35, so a same-day relaunch would land exactly there. Defer it to 14:40Z.
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            in_band = now_utc.weekday() < 5 and (9, 5) <= (now_utc.hour, now_utc.minute) < (14, 5)
            if in_band:
                at = now_utc.replace(hour=14, minute=40, second=0, microsecond=0)
                delay = int((at - now_utc).total_seconds())
                subprocess.Popen(["bash", "-c", f"sleep {delay}; cd {HERE}; python3 ops.py {role} "
                                                f">> {LOGS}/ops_cron.log 2>&1"],
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                healed.append(f"{role} relaunch DEFERRED to {at:%H:%M}Z (usage-window rule)")
            else:
                r = launch(role)
                healed.append(f"{role} relaunched ({'ok' if r.get('ok') else r.get('msg', '?')[:60]})")
            misses.remove(msg)
    if healed:
        subprocess.run([os.path.expanduser("~/maintenance/bin/notify.sh"), "stocks",
                        "Ops role missed its run — self-heal relaunched",
                        ("; ".join(healed))[:190]], check=False)
    if misses:
        try:
            sys.path.insert(0, str(HERE))
            import asks
            already = any(a["status"] == "open" and a["by"] == "ops-verify"
                          for a in asks.load()["asks"])
            if not already:
                asks.add("coo", "ops.py verify: scheduled role(s) left NO report — the "
                                "finish failed even if the launch succeeded: " + "; ".join(misses),
                         by="ops-verify", about="_engine/agent/ops.py",
                         why="A role that dies mid-flight skips its gates silently "
                             "(2026-08-20: numbers rebuilt 150 cards, no snapshot, no "
                             "report). Read its ops_<role>.log tail, salvage or revert "
                             "its working tree, and reopen what it left undone.")
        except Exception:
            pass
        subprocess.run([os.path.expanduser("~/maintenance/bin/notify.sh"), "stocks",
                        "Ops role missed its run",
                        ("; ".join(misses))[:190]], check=False)
    print(json.dumps({"ok": not misses, "misses": misses}))
    return 0 if not misses else 1


def launch(role):
    role = ROLE_ALIASES.get(role, role)
    ok, msg = runner.auth_check()
    if not ok:
        return {"ok": False, "msg": msg}
    OPS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if not NO_MCP.exists():
        NO_MCP.write_text(json.dumps({"_doc": "ops sessions get NO MCP servers — no broker, "
                                              "no external tools beyond the box", "mcpServers": {}}, indent=1))
    if role == "hunt":
        _hunt_ledger_sync()   # an area in the charter must be reachable in the ledger
    try:
        prompt = PROMPTS[role]() + single_turn_note()
    except Exception as e:   # a prompt with a hole is not a session; say so, do not launch
        return {"ok": False, "msg": f"prompt did not render: {type(e).__name__}: {e}"}
    cmd = [runner.CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions",
           "--strict-mcp-config", "--mcp-config", str(NO_MCP),
           # Each role's model comes from the org chart (roster.py), not from here.
           # numbers/signals=sonnet, coo/hunt=opus (David 2026-08-18: "Fixer should go down
           # to sonnet"; PROJECT_STANDARDS §2 per-job sign-off). The COO and hunt stay on
           # Opus because their whole value is catching what the cheaper roles got wrong.
           "--model", runner.job_model(role)]
    log = open(LOGS / f"ops_{role}.log", "w")
    subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=log,
                     start_new_session=True, env=runner.clean_env())
    return {"ok": True, "msg": f"ops {role} launched"}


if __name__ == "__main__":
    r = ROLE_ALIASES.get(sys.argv[1], sys.argv[1]) if sys.argv[1:] else ""
    if r == "verify":
        sys.exit(verify())
    if r in PROMPTS:
        out = launch(r)
        print(json.dumps(out))
        sys.exit(0 if out["ok"] else 1)
    sys.exit("usage: ops.py numbers|signals|coo|hunt|verify   (fixer = alias for numbers)")
