#!/usr/bin/env python3
"""
The VP — night sweep and prep desk for the PM. ZERO Claude tokens.

David, 2026-08-14: "this local model at night should sweep as much as possible and
ensure enough prep is ready for the PM. It should act as a VP now, review everything
and have the material ready for the PM when it turns on."

The nightly chain used to be three commands stapled together in a crontab line
(refresh_cards -> numwatch -> quality scan) whose only output was three log files
nobody read. This is the same work plus the intel and dossier legs, run in dependency
order, with one deliverable: data/vp_brief.md — the desk the PM sits down at.

DOCTRINE (token-efficiency skill, David 2026-08-11): everything here is code or a LOCAL
model. Local output is preparation and navigation, NEVER a filter — the VP narrows where
the PM looks and never decides what it may see. Every raw source stays fully available,
and the brief says plainly what it could not verify.

Stages (each degrades independently; one failure never blocks the rest):
  1 cards     refresh_cards.py     code   XBRL -> fincard.json per name
  2 numbers   numwatch.py run      LOCAL  audits the PM's own prose against the filings
  3 quality   quality.py scan      code   rolls findings into the queue
  4 intel     feeds.py refresh     code   news/filings/earnings/13D radar
  5 score     relevance.py         LOCAL  scores headlines for the held book
  6 triage    scout.py run         LOCAL  mechanism-scores the origination funnel
  7 dossiers  dossier.py build     LOCAL  quote-verified terms for every held name
  8 brief     diffbrief.py         code   the coded delta since the last session
  9 vp_brief  (this file)          code   consolidates 1-8 into the PM's desk

CLI: vp.py sweep [--fast]      --fast skips dossier rebuilds (stage 7)
     vp.py brief               rebuild vp_brief.md from existing artifacts only
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
ROOT = ENGINE.parent
DATA = HERE / "data"
NAMES = HERE / "names"
JOURNAL = HERE / "journal"
VENV = ENGINE / ".venv" / "bin" / "python"
PY = str(VENV) if VENV.exists() else "python3"


def _j(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _write(path, text):
    """Atomic — the PM may be reading this while the sweep rewrites it."""
    path = Path(path)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run(label, cmd, timeout=1800):
    """Run one stage. Never raises — a stage that fails is REPORTED, not fatal, because
    a half-swept desk the PM knows about beats a missing one it doesn't."""
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip().splitlines()
        ok = p.returncode == 0
        tail = out[-1][:200] if out else ""
        err = "" if ok else ((p.stderr or "").strip().splitlines() or [""])[-1][:200]
    except subprocess.TimeoutExpired:
        ok, tail, err = False, "", f"timed out after {timeout}s"
    except Exception as e:
        ok, tail, err = False, "", f"{type(e).__name__}: {e}"[:200]
    dur = time.time() - t0
    print(f"  [{'ok ' if ok else 'FAIL'}] {label:<10} {dur:6.1f}s  {tail or err}")
    return {"stage": label, "ok": ok, "seconds": round(dur, 1), "out": tail, "err": err}


# ---------------------------------------------------------------- the brief
def held_names():
    pf = _j(DATA / "portfolio.json", {})
    return [p["symbol"] for p in pf.get("positions", []) if p.get("symbol")]


def card_state(tk):
    """Freshness + flags for one name's code-computed numbers."""
    c = _j(NAMES / tk / "fincard.json", {})
    if not c:
        return {"has": False}
    flags = c.get("flags") or []
    F = c.get("figures") or {}
    stale = [k for k, v in F.items() if isinstance(v, dict) and v.get("STALE")]
    return {"has": True, "built": str(c.get("built") or c.get("built_at") or c.get("as_of") or "")[:19],
            "figures": len(F), "flags": flags, "stale": stale}


def numcheck_rows(tk):
    return (_j(NAMES / tk / "numcheck.json", {}) or {}).get("findings") or []


def brief(stages=None):
    ts = _now()
    pf = _j(DATA / "portfolio.json", {})
    thesis = _j(DATA / "thesis.json", {})
    feed = _j(DATA / "feed.json", {})
    q = _j(DATA / "quality_queue.json", {})
    cands = _j(DATA / "candidates.json", {}).get("items", {})
    held = held_names()

    L = []
    L.append(f"# VP prep brief — {ts}")
    L.append("")
    L.append("_Prepared by the VP night sweep: code + LOCAL models only, zero Claude tokens. "
             "This is PREPARATION, not a filter — every raw source named here is fully available "
             "and you should drill into it wherever a decision depends on it. Where I could not "
             "verify something I say so; do not read silence as clearance._")
    L.append("")

    # --- what actually ran, so the PM can trust or distrust each section
    if stages:
        L.append("## What I ran for you")
        L.append("")
        L.append("| stage | result | took | note |")
        L.append("|---|---|---|---|")
        for s in stages:
            L.append(f"| {s['stage']} | {'ok' if s['ok'] else '**FAILED**'} | {s['seconds']}s | "
                     f"{(s['out'] or s['err'])[:90].replace('|', '/')} |")
        bad = [s["stage"] for s in stages if not s["ok"]]
        L.append("")
        L.append(f"**{len(bad)} stage(s) failed: {', '.join(bad)} — treat anything downstream of "
                 f"them as UNPREPARED and do it yourself.**" if bad
                 else "**All stages clean.** The desk below is fully prepped.")
        L.append("")

    # --- feed integrity first: a stale section changes what silence means
    deg = feed.get("degraded") or []
    L.append("## Feed integrity")
    L.append("")
    if deg:
        aso = feed.get("as_of") or {}
        for sec in deg:
            L.append(f"- ⚠ **{sec}** did NOT refresh — carried over from {aso.get(sec, 'an earlier fetch')}. "
                     f"'No new {sec}' means UNKNOWN, not quiet.")
        if feed.get("vendor_errors"):
            L.append(f"- vendor errors: {'; '.join(feed['vendor_errors'][-3:])}")
    else:
        L.append(f"- All sections refreshed at {feed.get('fetched_at', '?')}. "
                 f"Universe: {', '.join(feed.get('universe') or []) or '—'}")
    L.append("")

    # --- the book, name by name: numbers, their audit, and the anchor
    L.append("## Your book — numbers prepped, and what I could not clear")
    L.append("")
    total_open = 0
    for p in pf.get("positions", []):
        tk = p.get("symbol")
        cs = card_state(tk)
        rows = numcheck_rows(tk)
        total_open += len(rows)
        th = (thesis or {}).get(tk) or {}
        L.append(f"### {tk} — {p.get('qty')} sh @ ${p.get('avg_cost')}, mark ${p.get('price')} "
                 f"({p.get('pnl_pct')}%)")
        if cs.get("has"):
            L.append(f"- fincard rebuilt {cs['built']} · {cs['figures']} figures"
                     + (f" · ⚠ {len(cs['flags'])} flags" if cs["flags"] else " · no flags")
                     + (f" · STALE: {', '.join(cs['stale'][:4])}" if cs["stale"] else ""))
            for f in cs["flags"][:3]:
                L.append(f"  - flag: {f[:150]}")
        else:
            L.append("- ⚠ **no fincard** — rebuild the dossier before you act on this name.")
        if th:
            L.append(f"- anchor: implied {th.get('implied_value')} · deadline {th.get('deadline')} "
                     f"· last re-underwrite {th.get('last_reunderwrite', '—')}")
        if rows:
            L.append(f"- **numbers watchdog: {len(rows)} open finding(s) on YOUR prose** —")
            for r in rows[:6]:
                L.append(f"  - {r[:180]}")
            if len(rows) > 6:
                L.append(f"  - …and {len(rows) - 6} more in `names/{tk}/numcheck.json`")
        else:
            L.append("- numbers watchdog: clean.")
        L.append("")
    L.append(f"_Watchdog blind spot: it compares against the SUBJECT's fincard, so a correctly-cited "
             f"third-party figure flags UNSOURCED. 'Cited to <third party> and verified in the memo "
             f"audit' clears a row — do not re-derive it._")
    L.append("")

    # --- the funnel, pre-triaged so the PM spends judgment not search
    counts = q.get("counts") or {}
    L.append("## Data-quality queue")
    L.append("")
    L.append(f"- open {counts.get('open', 0)} · planned {counts.get('planned', 0)} "
             f"· accepted {counts.get('accepted', 0)} · resolved {counts.get('resolved', 0)}")
    L.append("")

    ranked = sorted(
        [c for c in cands.values() if (c.get("status") or "") in ("pre_triaged", "triaged", "new")],
        key=lambda c: -((c.get("triage") or {}).get("score") or (c.get("pre") or {}).get("plausible") or 0))
    L.append("## Origination funnel — top of the scout queue")
    L.append("")
    if ranked:
        L.append("| score | kind | ticker | what | status |")
        L.append("|---|---|---|---|---|")
        for c in ranked[:12]:
            sc = (c.get("triage") or {}).get("score") or (c.get("pre") or {}).get("plausible") or 0
            L.append(f"| {sc} | {c.get('kind', '')} | {c.get('ticker') or '—'} | "
                     f"{str(c.get('detail', ''))[:70].replace('|', '/')} | {c.get('status', '')} |")
        L.append("")
        L.append("_A triage score is a LEAD, never a thesis. Record your verdict ON the candidate "
                 "(status pm_reviewed + pm_note) so the funnel remembers._")
    else:
        L.append("_Queue empty — nothing pre-triaged since the last sweep._")
    L.append("")

    # --- what the VP is explicitly handing over
    L.append("## Handover — what is ready, and what needs your judgment")
    L.append("")
    L.append("Ready (done, cite it rather than redoing it):")
    L.append(f"- fincards rebuilt for {len([t for t in held if card_state(t).get('has')])}/{len(held)} held names")
    L.append(f"- your prose audited against the filings — {total_open} open finding(s) listed above")
    L.append(f"- intel refreshed; headlines scored; {len(ranked)} candidates pre-triaged with mechanisms attached")
    L.append(f"- dossiers/terms available under `names/<TK>/` for every held name")
    L.append(f"- the coded delta since your last session is in `data/session_brief.md`")
    L.append("")
    L.append("Needs YOUR judgment (I am code and a local model — I do not decide):")
    L.append("- every open watchdog finding above: source it, fix it, or clear it with a reason")
    L.append("- the rotating re-underwrite: oldest `last_reunderwrite` in thesis.json")
    L.append("- the funnel: which leads earn real underwriting, which get dropped with a note")
    L.append("- anything a flag or STALE marker touches — a flagged number is not a number yet")
    L.append("")
    _write(DATA / "vp_brief.md", "\n".join(L))
    return DATA / "vp_brief.md", total_open



# ---------------------------------------------------------------- the review pass
# David, 2026-08-18: "promote the VP to the best claude sonnet model for now. give it a
# little boost in intelligence until things run smoothly."
#
# The sixteen stages above are code and local models and still cost zero Claude tokens.
# What they cannot do is JUDGE their own output: a stage table can say "numwatch: 99
# findings" but not "94 of those are the same label-matching defect and here are the 5
# that are real". That triage is what the PM was doing by hand every session, at Opus
# prices, on material a cheaper model can sort.
#
# HARD RULE, unchanged: this is PREPARATION, NEVER A FILTER. The review is APPENDED to
# the brief; it may not delete, downgrade or hide a single coded finding. Everything it
# says is labelled as the VP's opinion so the PM can disagree with it, and if it fails
# the brief still stands exactly as the coded stages wrote it.
REVIEW_PROMPT = """You are the VP of an autonomous equity book at {root}. The night sweep just
finished. You have NO broker access, you place no orders, and you decide nothing — your PM reads
your work at 14:05 UTC and makes every call.

Read, in this order:
  {data}/vp_brief.md        the sweep you are reviewing (stages, per-name prep, findings)
  {data}/unknowns.md        numbers the machine knows it cannot vouch for
  {data}/bench_brief.md     the overnight whole-market read
  {agent}/names/<TK>/numcheck.json   the raw watchdog findings behind the counts

Then APPEND to {data}/vp_brief.md a section headed exactly:

## VP review — judgment layer (Sonnet, not code)

with these four parts, tight, no preamble:

1. **What actually needs the PM this session** — at most 5 items, each one line, ranked. This is
   the whole point of you: the sweep produces hundreds of rows and only a handful move a decision.
2. **Watchdog triage** — group the open numwatch findings by ROOT CAUSE, give each group a count,
   and name the ones that are NOT structural noise. Be concrete: "37 = quarterly figure compared
   against a TTM card value" beats "many are false positives".
3. **What I could not verify** — anything in the sweep you could not stand behind, and why. If a
   stage FAILED, say what the PM must now do by hand.
4. **One process observation** — the thing you would change about this machine if it were yours.

RULES, absolute:
- **You may not delete, downgrade, reword or hide any coded finding.** Append only. If you think
  a coded flag is wrong, SAY SO in your section and leave the flag standing.
- **Every number you write is quoted from a document or computed by a tool, never estimated**
  (MANDATE rail 7). If you do not know a number, write UNKNOWN and name what would settle it.
- Cite the file each claim came from. Prefer counting to characterising.
- If you are unsure whether something matters, include it and say you are unsure. Under-reporting
  costs the PM more than over-reporting.
Finish with one line to stdout: "vp review: N items for the PM, M root-cause groups"."""


def review(timeout=1800):
    """Launch the VP's Sonnet review over the brief the coded stages just wrote."""
    sys.path.insert(0, str(ENGINE / "research"))
    import runner
    ok, msg = runner.auth_check()
    if not ok:
        print(f"  [FAIL] review     auth: {msg}")
        return {"stage": "review", "ok": False, "seconds": 0, "out": "", "err": msg[:200]}
    nomcp = ENGINE / "config" / "ops_mcp.json"
    if not nomcp.exists():
        nomcp.parent.mkdir(parents=True, exist_ok=True)
        nomcp.write_text(json.dumps({"_doc": "no MCP — the VP has no broker and no external tools",
                                     "mcpServers": {}}, indent=1))
    prompt = REVIEW_PROMPT.format(root=ROOT, data=DATA, agent=HERE)
    t0 = time.time()
    try:
        p = subprocess.run(["claude", "-p", prompt, "--dangerously-skip-permissions",
                            "--strict-mcp-config", "--mcp-config", str(nomcp),
                            "--model", runner.job_model("vp")],
                           cwd=str(ROOT), capture_output=True, text=True,
                           timeout=timeout, env=runner.clean_env())
        out = (p.stdout or "").strip().splitlines()
        ok2, tail = p.returncode == 0, (out[-1][:200] if out else "")
        err = "" if ok2 else ((p.stderr or "").strip().splitlines() or [""])[-1][:200]
    except subprocess.TimeoutExpired:
        ok2, tail, err = False, "", f"timed out after {timeout}s"
    except Exception as e:
        ok2, tail, err = False, "", f"{type(e).__name__}: {e}"[:200]
    dur = time.time() - t0
    print(f"  [{'ok ' if ok2 else 'FAIL'}] {'review':<10} {dur:6.1f}s  {tail or err}")
    return {"stage": "review", "ok": ok2, "seconds": round(dur, 1), "out": tail, "err": err}


def _asof_header(title):
    """Provenance block for the brief. Never fatal — a brief without a header
    beats no brief."""
    try:
        import asof
        return asof.header(title) + "\n"
    except Exception as e:
        return f"_(provenance header unavailable: {type(e).__name__})_\n\n"


def sweep(fast=False, bench_minutes=60, bench_fill=400, no_review=False):
    t0 = time.time()
    print(f"VP night sweep — {_now()}")
    stages = []
    stages.append(run("cards", [PY, "refresh_cards.py"], 900))
    stages.append(run("numbers", ["python3", "numwatch.py", "run"], 2400))
    stages.append(run("quality", ["python3", "quality.py", "scan"], 600))
    stages.append(run("intel", [PY, "feeds.py", "refresh"], 900))
    stages.append(run("score", ["python3", "relevance.py"], 900))
    stages.append(run("triage", ["python3", "scout.py", "run"], 1200))
    if not fast:
        for tk in held_names():
            stages.append(run(f"dossier:{tk}", ["python3", "dossier.py", "build", tk], 900))
    stages.append(run("brief", ["python3", "diffbrief.py"], 300))
    # The reading pass. Unlike every stage above it, this one is BOUNDED BY TIME, not by a
    # work list — it reads until the window closes. That is the point: the model is free and
    # the night is long (David 2026-08-14: "can run for hours every night"). The queue is
    # durable, so whatever it does not finish tonight is waiting tomorrow.
    stages.append(run("bench:fill", ["python3", "bench.py", "fill", "--limit", str(bench_fill)], 900))
    stages.append(run("bench:read", ["python3", "bench.py", "work",
                                     "--minutes", str(bench_minutes), "--model", "fast"],
                      bench_minutes * 60 + 300))
    stages.append(run("bench:rank", ["python3", "bench.py", "rank"], 300))
    # Price the top leads so the Funnel and the brief carry numbers, not bare tickers.
    stages.append(run("bench:cards", ["python3", "bench.py", "cards", "--limit", "15"], 1800))
    stages.append(run("bench:brief", ["python3", "bench.py", "brief"], 300))
    # Last, and deliberately last: the two files the PM opens BEFORE anything else. The
    # unknowns register has to run after the cards and the watchdog so it sees tonight's
    # flags, and the roster brief has to run after everything so its freshness column
    # describes the night that just happened rather than the one before it.
    stages.append(run("unknowns", ["python3", "unknowns.py", "scan"], 600))
    stages.append(run("roster", ["python3", "roster.py", "brief"], 300))
    # Freshness of every desk input, on the two clocks that matter — market time (stale in
    # minutes, and only while the market is open) and filing time (updates once a quarter,
    # current until the issuer files again). Non-fatal: it reports, the PM decides.
    stages.append(run("asof", ["python3", "asof.py", "check"], 300))
    stages.append(run("filings", ["python3", "asof.py", "filings"], 600))
    path, nfind = brief(stages)
    # The review reads the brief, so it must run after brief() writes it — and it appends
    # rather than being folded in, so a review failure leaves the coded brief intact.
    if not fast and not no_review:
        stages.append(review())
    bad = [s["stage"] for s in stages if not s["ok"]]
    print(f"\nVP sweep done in {(time.time() - t0) / 60:.1f} min · "
          f"{len(stages) - len(bad)}/{len(stages)} stages ok · {nfind} open watchdog finding(s)")
    print(f"-> {path}")
    # Silent exit-0 failure is the cardinal sin (PROJECT_STANDARDS §1). Until now the VP
    # could die entirely at 20:40 and the first anyone knew was the PM opening a stale
    # brief sixteen hours later. A FAILED stage means the PM must do that work by hand,
    # which it can only do if it is told — so failures go to `alerts`, on state, not on
    # every run.
    if bad:
        try:
            subprocess.run([str(pathlib.Path.home() / "maintenance/bin/notify.sh"), "alerts",
                            "VP night sweep", f"{len(bad)}/{len(stages)} stage(s) FAILED: "
                            f"{', '.join(bad)[:160]} — the PM's desk is incomplete for tomorrow"],
                           capture_output=True, timeout=30)
        except Exception as e:
            print(f"  (alert push failed: {type(e).__name__})")
    return 1 if bad else 0


if __name__ == "__main__":
    a = sys.argv[1:2]
    if a == ["sweep"]:
        m = int(sys.argv[sys.argv.index("--bench-minutes") + 1]) if "--bench-minutes" in sys.argv else 60
        sys.exit(sweep(fast="--fast" in sys.argv, bench_minutes=m,
                       no_review="--no-review" in sys.argv))
    if a == ["review"]:
        r = review()
        sys.exit(0 if r["ok"] else 1)
    if a == ["brief"]:
        p, n = brief()
        print(f"{p} ({n} open findings)")
        sys.exit(0)
    sys.exit("usage: vp.py sweep [--fast] | vp.py brief")
