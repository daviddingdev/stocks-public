#!/usr/bin/env python3
"""
The Bench — durable work queue + stateless local-model workers. Spec: BENCH.md.

David, 2026-08-14: "i want the local model heavily utilized, can run for hours every
night given it's free. i notice local models never run more than 10 mins. should figure
out an architecture to let it keep running when it gets tired."

It is not fatigue. WE HAVE NEVER GIVEN IT MORE THAN TEN MINUTES OF WORK — numwatch
enumerates 5 names and exits, navindex 10 docs and exits. Work is fixed at process
start, so the process ends when the list does. Separately ollama unloads the model after
~5 min idle, so every script pays a cold load.

The fix is here: a durable queue and stateless workers.
  * one row per read-task, atomic writes, survives restarts
  * a worker LEASES a task, makes ONE FRESH-CONTEXT model call, writes the result, repeats
  * each task gets a clean context — the model is stateless, the QUEUE holds the state.
    That is what makes a 10-hour run possible: the long-LLM failure mode is shared context
    drifting, and there is no shared context here.
  * an expired lease returns to pending, so a killed worker loses one task, not the night
  * keep_alive keeps the model resident between tasks instead of cold-loading each one
  * workers are independent processes; run N in parallel if the box has headroom

CLI:
  bench.py fill [--limit N]        build read-tasks from the readable set
  bench.py work [--minutes M] [--model fast|dense] [--worker NAME] [--think|--no-think]  (default: data/bench_mode.json)
  bench.py status                  queue counts + throughput
  bench.py rank                    -> data/bench.json, ranked by a coded specificity score
                                    (bench.py-030) — not model confidence, not multiples
  bench.py question "<text>"       set the nightly ask only (shorthand for --ask)
  bench.py question --ask "..." [--rule "..."] [--channel "..."] [--schema "..."]
                                    set any/all of the question's fields (the PM owns this) —
                                    always update rule+channel together with ask if the new
                                    ask changes what "gap found" should mean
"""
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
DATA = HERE / "data"
NAMES = HERE / "names"
QUEUE = DATA / "bench_queue.json"
QFILE = DATA / "bench_question.json"
BENCH = DATA / "bench.json"
BENCH_BRIEF = DATA / "bench_brief.md"

sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
import models  # noqa: E402  — Mission Control's local-model registry
import gpu     # noqa: E402  — Mission Control's GPU queue (priority: Stocks first)
sys.path.insert(0, str(HERE))
from feeds import funnel_record  # noqa: E402  — scout.py-172 funnel counts

OLLAMA = models.chat_url()
# --model fast|dense stays the CLI contract (cron passes it); the tags behind it live in
# ~/maintenance/config/models.json. `fast` maps to the `bulk` ROLE — kept distinct from
# `dense` so a genuinely faster model can win the Stage-C sweep back later — but as of
# 2026-08-16 both roles resolve to the same model. See BENCH.md §3.
ROLES = {"fast": "bulk", "dense": "dense"}
KEEP_ALIVE = "60m"          # stay resident between tasks — no cold load per task
LEASE_S = 900               # a task leased longer than this is presumed dead
CHUNK = 24_000              # chars per model call (measured: 5.2s fast / 15.6s dense)
MODE_FILE = DATA / "bench_mode.json"   # optional read-mode overrides: think, num_predict, chunk
THINK_HEADROOM = 2500       # ollama counts thinking tokens against num_predict


def mode():
    """Read-mode overrides (data/bench_mode.json), so a night can run in THINK mode without a
    code change and the next morning's bench_queue.json rows say which mode produced them.
    Audit 2026-09-07: qwen3.8:27b is a thinking model and every call ran think:false."""
    m = _j(MODE_FILE, None) or {}
    return {"think": bool(m.get("think", False)), "num_predict": int(m.get("num_predict", 400)),
            "chunk": int(m.get("chunk", CHUNK))}

DEFAULT_QUESTION = {
    "channel": "11 — narrative-vs-contract gap",
    "ask": ("You are screening for the NARRATIVE-VS-CONTRACT gap: a company whose public story "
            "(secular decline, commoditization, patent cliff, melting ice cube, dying end market) "
            "is contradicted by its own contracts, backlog, renewal terms, royalty structure or "
            "unit economics as disclosed in this filing."),
    "schema": ('{"gap_found": true|false, "narrative": "<the story the market likely holds, <=20 words>", '
               '"contradicting_disclosure": "<VERBATIM sentence copied from the text, or empty>", '
               '"why_it_matters": "<=25 words>", "confidence": 0-10}'),
    "rule": ("If you cannot copy a VERBATIM contradicting sentence out of the text, gap_found MUST "
             "be false. Never paraphrase into the quote field. Never infer beyond the excerpt."),
    "claim_key": "gap_found",
    "set_by": "default", "set_at": None,
}


# ---------------------------------------------------------------- io
def _j(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _write(path, obj):
    """Atomic — a worker may be reading this while another writes it."""
    tmp = Path(str(path) + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def question():
    q = _j(QFILE, None)
    return q if q else DEFAULT_QUESTION


# ---------------------------------------------------------------- the model
_JSON_RE = re.compile(r"\{.*\}", re.S)

def ask_json(prompt, model, num_predict=400, timeout=1200, think=False):
    """One fresh-context call. qwen3.8 wraps output in ```json fences, so parse the
    outermost object rather than trusting the envelope.

    The slot is taken per call, not per run: an 800-read night must not lock the box's
    only GPU for five hours, and releasing between reads is what lets a higher-priority
    job (or David at a terminal) in without killing a generation mid-flight."""
    # format:"json" (audit 2026-09-07): without it every unparseable row in bench.log was a
    # TRUNCATED object — a real hit thrown away. num_ctx explicit from the registry: the
    # server is sized at 256K today; an unrequested window is a default that can change.
    opts = {"num_predict": num_predict + (THINK_HEADROOM if think else 0), "temperature": 0.2}
    ctx = models.options(ROLES["fast"]).get("num_ctx")
    if ctx:
        opts["num_ctx"] = int(ctx)
    body = json.dumps({"model": model, "think": bool(think), "stream": False,
                       "keep_alive": KEEP_ALIVE, "format": "json", "options": opts,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
    t0 = time.time()
    with gpu.slot(job="the Bench", model=model):
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    gpu.record_usage(job="the Bench", model=model, prompt_tokens=d.get("prompt_eval_count"),
                     output_tokens=d.get("eval_count"), seconds=time.time() - t0)
    raw = d["message"]["content"]
    m = _JSON_RE.search(raw or "")
    if not m:
        return None, raw
    try:
        return json.loads(m.group(0)), raw
    except Exception:
        return None, raw


# ---------------------------------------------------------------- queue
def _load():
    return _j(QUEUE, {"_doc": "bench read-queue — see BENCH.md", "tasks": {}, "created": _now()})



# ---------------------------------------------------------------- STAGE A: the universe
# David, 2026-08-15: "it clearly targeted owned stocks plus watchlist. why is that? the
# score should be unbiased and looking at every stock." He was right and it was structural:
# fill() sourced its corpus from names/ (held + watched dossiers) and */research/_evidence/
# (names already torn down), so it could only ever rediscover what we already own. Stage A
# builds the corpus from the WHOLE MARKET instead, and the book gets no privileged position
# in it — held names are read on the same footing as everything else, or not at all.
CORPUS = DATA / "bench_corpus"
UNIVERSE = DATA / "bench_universe.json"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from edgar_identity import UA  # SEC contact identity, config-driven


def _stamp():
    """Provenance stamp so this file's CONTENT drift is measurable later, not just its age."""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        import asof
        return asof.stamp()
    except Exception:
        return ""


REV_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")


def _get(url, tries=3):
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _frame(tag, period):
    try:
        d = json.loads(_get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{period}.json"))
        return {r["cik"]: r["val"] for r in d.get("data", [])}
    except Exception:
        return {}


_TAG_RE = re.compile(rb"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

def _strip(raw):
    txt = _TAG_RE.sub(b" ", raw).decode("utf-8", "ignore")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&#8217;", "'"), ("&#8220;", '"'),
                 ("&#8221;", '"'), ("&#151;", "-"), ("&lt;", "<"), ("&gt;", ">")):
        txt = txt.replace(a, b)
    return _WS_RE.sub(" ", txt)


def universe(min_rev=50e6, max_rev=20e9):
    """Every SEC filer with real disclosed revenue in the band. One frames request per tag
    covers the whole market — no per-name fetching, no vendor, no survivorship filter."""
    rev = {}
    for tag in REV_TAGS:
        for per in ("CY2024", "CY2025"):
            rev.update(_frame(tag, per))
    m = json.loads(_get("https://www.sec.gov/files/company_tickers.json"))
    by_cik = {int(v["cik_str"]): v["ticker"].upper() for v in m.values()}
    names = {int(v["cik_str"]): v["title"] for v in m.values()}
    out = {}
    for cik, v in rev.items():
        if not v or not (min_rev <= v <= max_rev):
            continue
        tk = by_cik.get(int(cik))
        if not tk or not tk.isalpha():      # drop units/warrants/preferreds
            continue
        out[tk] = {"ticker": tk, "cik": int(cik), "revenue": v, "name": names.get(int(cik), "")}
    _write(UNIVERSE, {"built": _now(), "band": [min_rev, max_rev], "count": len(out),
                      "_doc": "Whole-market readable set. The book gets no privileged place here.",
                      "names": out})
    funnel_record("bench:universe", len(rev), len(out))
    print(f"universe: {len(out)} companies with ${min_rev/1e6:.0f}M-${max_rev/1e9:.0f}B revenue")
    return out


def fetch(limit=120, forms=("10-K", "10-Q")):
    """Pull the latest 10-K/10-Q text for universe names we have not read yet.
    EDGAR-polite; resumable — it only ever fetches what is missing."""
    uni = _j(UNIVERSE, {}).get("names") or {}
    if not uni:
        print("no universe — run: bench.py universe"); return 0
    CORPUS.mkdir(parents=True, exist_ok=True)
    todo = [v for tk, v in sorted(uni.items()) if not (CORPUS / tk).exists()]
    random.Random(7).shuffle(todo)          # unbiased order, stable across runs
    got = 0
    for u in todo[:limit]:
        tk, cik = u["ticker"], u["cik"]
        d = CORPUS / tk
        try:
            sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
            rec = sub["filings"]["recent"]
            picked = {}
            for form, date, acc, doc in zip(rec["form"], rec["filingDate"],
                                            rec["accessionNumber"], rec["primaryDocument"]):
                if form in forms and form not in picked and doc:
                    picked[form] = (date, acc, doc)
                if len(picked) == len(forms):
                    break
            if not picked:
                d.mkdir(parents=True, exist_ok=True)
                (d / ".nofilings").write_text(_now()); continue
            d.mkdir(parents=True, exist_ok=True)
            for form, (date, acc, doc) in picked.items():
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                txt = _strip(_get(url))[:3_000_000]
                (d / f"{date}_{form.replace('/', '')}.txt").write_text(txt)
                time.sleep(0.15)            # EDGAR fair-access
            got += 1
            if got % 20 == 0:
                print(f"  fetched {got}…")
        except Exception as e:
            print(f"  skip {tk}: {str(e)[:70]}")
            time.sleep(0.3)
    have = sum(1 for x in CORPUS.iterdir() if x.is_dir()) if CORPUS.exists() else 0
    funnel_record("bench:fetch", len(todo), got)
    print(f"fetched {got} companies · corpus now {have} of {len(uni)} universe names")
    return got


# Where contract/business disclosure actually lives. The first test read 18 cover pages,
# 13 proxy statements and 6 press releases, and correctly found nothing — the model was
# disciplined, the CORPUS was wrong. Stage B is targeted section extraction, not chunk 0..3
# of whatever file happened to be on disk.
SECTION_RE = re.compile(
    r"(item\s*1\s*[\.\-–—:]?\s*business"
    r"|item\s*7\s*[\.\-–—:]?\s*management.s discussion"
    r"|item\s*2\s*[\.\-–—:]?\s*management.s discussion"
    r"|revenue\s+recognition"
    r"|commitments\s+and\s+contingencies"
    r"|concentrations?\s+of\s+(credit\s+)?risk"
    r"|customer\s+concentration"
    r"|remaining\s+performance\s+obligation"
    r"|backlog"
    r"|royalt(y|ies)"
    r"|collaboration\s+(and\s+license\s+)?agreements?"
    r"|license\s+agreements?)", re.I)

# Forms whose text is compensation tables or press-release boilerplate: no contract disclosure.
SKIP_FORMS = ("DEF14A", "DEF 14A", "8-K", "8K", "S-1", "424B")


def _targets(path, max_windows=4):
    """Byte windows around disclosure that can actually answer the question."""
    try:
        txt = path.read_text(errors="ignore")
    except Exception:
        return []
    if len(txt) < 8000:
        return []
    hits, seen = [], []
    for m in SECTION_RE.finditer(txt):
        st = max(0, m.start() - 1500)
        if any(abs(st - s) < CHUNK for s in seen):    # don't re-read overlapping windows
            continue
        seen.append(st)
        hits.append(st)
        if len(hits) >= max_windows:
            break
    return hits


def _chan_id(channel_str):
    """bench.py-176: the stable leading token of a channel string (e.g. '11' from
    '11 — narrative-vs-contract gap'), immune to a whitespace/wording edit to the
    description after the id. rank()/_channel_stats() used to compare the FULL channel
    string verbatim against bench_question.json's current value, so any edit to the
    description text — not just a real channel switch — zeroed bench.json and orphaned
    every task stamped under the old wording (16,147 of them, permanently: rank()'s
    filter is exact-match and a task's stamped channel never gets rewritten). Comparing
    on this leading-token id instead means only an actual id change opens a new
    namespace; a copyedit to the description does not."""
    m = re.match(r"^\s*(\S+)", channel_str or "")
    return m.group(1) if m else (channel_str or "?")


def _qkey(qn):
    """A stable short id for the active question, used to scope task ids (bench.py-072)."""
    return _chan_id(qn.get("channel"))


def fill(limit=40):
    """Stage A/B: pick filings worth reading, then the WINDOWS worth reading inside them.

    bench.py-072: task ids used to be bare f"{tk}:{f.name}:@{off}" -- no question in the id
    at all. The FIRST question to read a window "owned" that id forever: once it was marked
    done, every later question (bench_question.json changed by the PM, e.g. 11 -> 13) hit
    `if tid in tasks: continue` on the SAME id and could never queue that window again. 141
    of 16291 tasks in the live queue carried the current question's channel; the other
    16150 were pre-058 legacy reads of a DIFFERENT (untagged) question, permanently
    blocking re-reads. fill() reported "0 queued" and work() reported "queue drained" every
    night since the PM set channel 13 on 2026-08-26 -- a stalled queue that looked, from the
    log line alone, exactly like a finished one. Scoping the id by the active question's key
    makes a question change open a fresh namespace instead of colliding with the last one's."""
    q = _load()
    tasks = q["tasks"]
    qkey = _qkey(question())
    added = skipped = 0
    cands = []
    if CORPUS.exists():                      # STAGE A corpus — the whole market
        for d in sorted(CORPUS.iterdir()):
            if d.is_dir():
                cands += [(d.name, f) for f in sorted(d.glob("*.txt"))]
    if not cands:                            # fallback only if Stage A has not run yet
        for d in sorted(NAMES.iterdir()) if NAMES.exists() else []:
            if d.is_dir() and (d / "filings").exists():
                cands += [(d.name, f) for f in sorted((d / "filings").glob("*.txt"))]
    random.Random(0).shuffle(cands)
    for tk, f in cands:
        if any(k.lower() in f.name.lower() for k in SKIP_FORMS):
            skipped += 1
            continue
        for off in _targets(f):
            tid = f"q{qkey}:{tk}:{f.name}:@{off}"
            if tid in tasks:
                continue
            tasks[tid] = {"id": tid, "ticker": tk, "file": str(f), "offset": off,
                          "state": "pending", "added": _now()}
            added += 1
            if added >= limit:
                _write(QUEUE, q)
                funnel_record("bench:fill", len(cands), added)
                print(f"queued {added} targeted read-tasks · skipped {skipped} non-substantive filings")
                return added
    _write(QUEUE, q)
    funnel_record("bench:fill", len(cands), added)
    print(f"queued {added} targeted read-tasks · skipped {skipped} non-substantive filings")
    return added


def _lease(q, worker):
    """Take one pending task, or reclaim one whose lease expired (a dead worker)."""
    now = time.time()
    for t in q["tasks"].values():
        if t["state"] == "pending":
            t.update(state="leased", worker=worker, leased_at=now)
            return t
    for t in q["tasks"].values():   # reclaim: a killed worker loses one task, not the night
        if t["state"] == "leased" and now - (t.get("leased_at") or 0) > LEASE_S:
            t.update(state="leased", worker=worker, leased_at=now,
                     reclaimed=(t.get("reclaimed", 0) + 1))
            return t
    return None


def work(minutes=60, model_key="fast", worker=None, think=None):
    worker = worker or f"w{os.getpid()}"
    md = mode()
    if think is not None:
        md["think"] = bool(think)
    # Pre-check here, not at import: `fill` and `rank` need no model and must still run
    # when ollama is down. A dead reader, though, should die before it leases a task.
    model = models.require(ROLES[model_key], job="bench work")
    qn = question()
    deadline = time.time() + minutes * 60
    done = errs = hits = kept = 0
    t_start = time.time()
    print(f"bench worker {worker} · {model} · until {minutes}m · question: {qn['channel']} · "
          f"mode: think={md['think']} num_predict={md['num_predict']} chunk={md['chunk']}")
    while time.time() < deadline:
        q = _load()
        t = _lease(q, worker)
        if not t:
            print("queue drained"); break
        _write(QUEUE, q)
        try:
            txt = Path(t["file"]).read_text(errors="ignore")
            off = t.get("offset", t.get("chunk", 0) * CHUNK)
            chunk = txt[off:off + md["chunk"]]
            method = ("\nMETHOD: read the whole excerpt first. List to yourself every sentence that "
                      "states the figure the question asks for, with its date; discard the ones the "
                      "RULE rejects; then copy the one that survives CHARACTER-FOR-CHARACTER. A "
                      "quote you did not see printed is a failure.\n") if md["think"] else ""
            prompt = (f"{qn['ask']}\n\nAnswer strictly as JSON:\n{qn['schema']}\n{qn['rule']}{method}\n\n"
                      f"TICKER: {t['ticker']}\nDOCUMENT: {Path(t['file']).name}\n\n{chunk}")
            t0 = time.time()
            out, raw = ask_json(prompt, model, num_predict=md["num_predict"], think=md["think"])
            dt_s = round(time.time() - t0, 1)
            if out is None:
                raise ValueError(f"unparseable: {(raw or '')[:80]}")
            # GUARDRAIL, enforced by CODE not by the model: a claimed gap must carry a
            # verbatim sentence that actually appears in the source text.
            quote = (out.get("contradicting_disclosure") or "").strip()
            # bench.py-066: the claim key follows the QUESTION, not a constant — a
            # renamed schema boolean must not silently zero every read
            claimed = bool(out.get(qn.get("claim_key", "gap_found")))
            verbatim = bool(quote) and _norm(quote) in _norm(chunk)
            if claimed:
                hits += 1
            # signals-148: a claimed+verbatim quote can still be a disclaimer sentence with
            # no dollar figure in it (backlog_usd landed elsewhere in the JSON, unverified).
            # amount_key names the schema field carrying the dollar figure; require its
            # digits to actually appear somewhere in the chunk the model read, not just be
            # asserted. min_filing_date rejects a survivor whose filing predates the
            # channel's cutoff (bench.py-059 corpus mixes vintages; HOLOW's 2023-04-28 10-Q
            # survived the old gate). Both are opt-in per question — absent, the check is a
            # no-op, so questions with no dollar-figure or vintage requirement are unaffected.
            amount_key = qn.get("amount_key")
            amount_ok = True
            if amount_key:
                amt = _norm_amount(out.get(amount_key))
                amount_ok = bool(amt) and amt in _norm_amount(chunk)
            min_date = qn.get("min_filing_date")
            date_ok = True
            if min_date:
                fdate = _filing_date(t["file"])
                date_ok = bool(fdate) and fdate >= min_date
            survived = claimed and verbatim and amount_ok and date_ok
            if survived:
                kept += 1
            q = _load(); tt = q["tasks"][t["id"]]
            # bench.py-058: stamp WHICH question produced this row — the queue is durable
            # and outlives any single channel, so a task's own "done" record is the only
            # place that can later say it was channel 11's read, not channel 12's.
            tt.update(state="done", model=model, seconds=dt_s, at=_now(), channel=qn["channel"],
                      result=out, claimed=claimed, verbatim=verbatim, survived=survived,
                      think=md["think"], chunk=md["chunk"])
            _write(QUEUE, q)
            done += 1
            if survived:
                print(f"  HIT {t['ticker']:<6} {quote[:90]}")
        except Exception as e:
            q = _load(); tt = q["tasks"].get(t["id"])
            if tt:
                n = tt.get("attempts", 0) + 1
                tt.update(state="pending" if n < 3 else "failed", attempts=n,
                          error=str(e)[:160], at=_now())
                _write(QUEUE, q)
            errs += 1
            print(f"  err {t['id'][:40]}: {str(e)[:90]}")
    el = (time.time() - t_start) / 60
    rate = done / el if el else 0
    funnel_record("bench:read", done + errs, done)
    funnel_record("bench:claimed", done, hits)
    funnel_record("bench:survived", hits, kept)
    print(f"\nworker {worker}: {done} read · {errs} err · {hits} claimed · {kept} SURVIVED "
          f"verbatim check · {el:.1f}m · {rate:.1f} reads/min")
    if hits:
        print(f"survival rate {100*kept//hits}% — a low rate means the prompt is too loose")
    return done


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _norm_amount(s):
    """Strip the currency symbol and collapse whitespace so '$ 384,489' and '$384,489'
    compare equal against the same treatment applied to the surrounding chunk. A pure
    digit-collapse (comparing only 7,7 vs everything else in the chunk) was tried first
    and rejected: on the channel-11 corpus it let ACNT's '$8.4 million' and SDRL's
    '$2.4 billion' match on '84'/'24' turning up elsewhere in an unrelated number inside
    a 24k-char chunk — exactly the two rows the PM's own manual check found absent from
    the filing. Keeping the amount as a normalized PHRASE avoids that collision."""
    return re.sub(r"\s+", " ", (s or "").replace("$", "").lower()).strip()


_FILE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

def _filing_date(file_path):
    """bench.py-059 corpus mixes filing vintages; the date is the filename's leading
    YYYY-MM-DD (see fill()'s naming), not anything the model is asked to read."""
    m = _FILE_DATE_RE.match(Path(file_path).name)
    return m.group(1) if m else None


def status():
    q = _load(); tasks = q["tasks"]
    c = {}
    for t in tasks.values():
        c[t["state"]] = c.get(t["state"], 0) + 1
    dn = [t for t in tasks.values() if t["state"] == "done"]
    sec = sum(t.get("seconds") or 0 for t in dn)
    print(f"queue: {len(tasks)} tasks · " + " · ".join(f"{k} {v}" for k, v in sorted(c.items())))
    if dn:
        print(f"read {len(dn)} chunks in {sec/60:.1f} model-minutes "
              f"({sec/len(dn):.1f}s each) · claimed {sum(1 for t in dn if t.get('claimed'))} "
              f"· survived {sum(1 for t in dn if t.get('survived'))}")
    print(f"question: {question()['channel']} (set_by {question().get('set_by')})")


# bench.py-030 (pm, 2026-08-20): the model's own "confidence" field is saturated —
# 494 rows sampled 2026-08-21 landed 8/9/10 only, with 84% at exactly 9, so the page-one
# ranking (best desc) was really ordering by quote COUNT, not by strength, and the header's
# claim to rank "by EVIDENCE" was false in practice. The PM's own diagnostic: check whether
# the per-quote judgment has variance BEFORE building a new score on top of it — it does not
# discriminate at the top, so per the PM's instruction ("if the local model cannot produce a
# discriminating score... rank on something coded instead") this replaces confidence as the
# RANKING key with a coded specificity score: does the quote carry a concrete, checkable
# figure (a dollar amount or percentage) AND name an actual change to a prior arrangement
# (terminated, impaired, breached, written off, ...)? A quote with both is a checkable,
# consequential fact — the kind the PM asked this channel to surface, rare by construction
# (14/494 rows scored the max on the corpus this was tuned against). Model confidence is
# kept on each evidence item for reference; it no longer drives order.
_NUM_RE = re.compile(r"(\$[\d,.]+\s?(million|billion|thousand)?|\d[\d,]*\.?\d*\s?%)", re.I)
_CHANGE_RE = re.compile(
    r"\b(terminat\w*|discontin\w*|withdr\w*|ceas\w*|expir\w*|cancel\w*|breach\w*|default\w*|"
    r"impair\w*|write.?off\w*|(?<!amended and )(?<!amended & )restat\w*|resign\w*|depart\w*|"
    r"non.?renewal|not\s+renew\w*|declin\w*|delist\w*|bankrupt\w*|covenant\s+violat\w*|"
    r"not\s+in\s+compliance|noncomplian\w*|non.compliance|going\s+concern|material\s+weakness)\b",
    re.I)
_DATE_RE = re.compile(r"\b(20\d{2}|january|february|march|april|may|june|july|august|september|"
                      r"october|november|december)\b", re.I)

# bench.py-087 (pm, 2026-08-31): _CHANGE_RE alone rewards boilerplate risk-factor language —
# "our ... Credit Agreement requires us to comply with a total indebtedness to capitalization
# ratio not to exceed 65%" scored 10/10 (a % figure plus "Restated" false-matching restat\w* in
# "Amended and Restated Credit Agreement") while FLUX's "we are currently in default under the
# Revolving Note" scored 6/10 for lacking a dollar figure. A change-word wrapped in hypothetical/
# conditional framing ("as an example", "requires us to", "not to exceed", "in the event of") is
# a covenant DESCRIPTION, not a breach; ACTUAL/PAST-tense framing ("we are in default", "were not
# in compliance", "has occurred") is the live event this channel exists to find and now scores on
# actuality first, a bare figure+change-verb second.
_HYPOTHETICAL_RE = re.compile(
    r"\b(as an example|for example|in the event (of|that)|would (be|result|trigger|require|occur)\w*|"
    r"requires? us to|require\w* the company to|if we (fail|are unable|do not)|not to exceed|"
    r"may (result|trigger|require)\w*|could (result|trigger|require)\w*|"
    r"(?:is|are) required to maintain)\b", re.I)
_ACTUAL_RE = re.compile(
    r"\b(we are (currently )?in default|(?:is|are|was|were) (currently )?in default|"
    r"(?:was|were) not in compliance|not in compliance with|noncomplian\w*|"
    r"(?:has|have) occurred|(?:was|were) in violation|"
    r"event of default (?:has|had) occurred|declared (?:a |an )?(?:event of )?default)\b", re.I)


def _is_hypothetical(quote):
    """bench.py-093: _HYPOTHETICAL_RE alone missed a mid-sentence conditional whose 'if' sits
    well before the actual-sounding phrase it governs — INVX's 'Additionally, if at any time
    an Event of Default ... has occurred and is continuing or if Excess Availability ... is
    less than 20%, the Company must maintain ...' scored ACTUAL credit off '(?:has|have)
    occurred' even though that phrase is inside an unresolved 'if' clause, not a fact. Any
    'if' preceding the _ACTUAL_RE match (other than 'even if', which concedes the fact) means
    the actual-sounding phrase is conditional, not live."""
    if _HYPOTHETICAL_RE.search(quote):
        return True
    m = _ACTUAL_RE.search(quote)
    if m:
        prefix = quote[:m.start()]
        if re.search(r"\bif\b", prefix, re.I) and not re.search(r"\beven\s+if\b", prefix, re.I):
            return True
    return False


def _specificity(quote):
    """0-10, coded — see bench.py-030 note above `rank()`. Rare at the top by design.

    bench.py-087: actuality gates the change-word credit and outweighs a bare figure — see
    note above _HYPOTHETICAL_RE. ACTUAL carries 6 of the 10 points on its own (a live default
    with no dollar figure still beats a covenant description that has one); a bare change-word
    wrapped in hypothetical framing earns nothing."""
    if not quote:
        return 0
    num = bool(_NUM_RE.search(quote))
    date = bool(_DATE_RE.search(quote))
    wc = len(quote.split())
    length_ok = 8 <= wc <= 70
    hyp = _is_hypothetical(quote)
    if _ACTUAL_RE.search(quote) and not hyp:
        s = 6 + (2 if num else 0)
    elif _CHANGE_RE.search(quote) and not hyp:
        s = (4 if num else 0) + 4
    else:
        s = 4 if num else 0
    if date:
        s += 1
    if length_ok:
        s += 1
    return min(s, 10)


def rank():
    """bench.json — ranked by a CODED specificity score (bench.py-030), never by a multiple.

    bench.py-072: this used to aggregate EVERY done+survived task regardless of which
    question produced it, so a PM reading bench.json for the currently-set question (e.g.
    channel 13, covenant/default) was actually shown channel-11 narrative-vs-contract
    quotes with no per-row signal that they answered a different question -- confirmed live:
    1271 rows, only 5 carrying any channel-13 evidence, yet the top-ranked rows (VRRM, OLN,
    SMXT, RDN...) were 100% channel=None (pre-058 legacy) quotes about an unrelated question.
    brief()'s own header already computed and disclosed a 'carried over from earlier
    channels' count for exactly this reason, but nothing stopped the carried-over rows from
    being ranked first and displayed as this run's leads. Filtering to the CURRENT channel
    here, at the source, means bench.json only ever holds evidence for the question actually
    on the PM's desk right now — matching _channel_stats()'s existing per-channel filter."""
    q = _load()
    cur_channel = question()["channel"]
    cur_key = _chan_id(cur_channel)
    by = {}
    n_matched = 0
    for t in q["tasks"].values():
        if t.get("state") != "done" or not t.get("survived") or _chan_id(t.get("channel")) != cur_key:
            continue
        n_matched += 1
        r = t["result"]
        row = by.setdefault(t["ticker"], {"ticker": t["ticker"], "evidence": [], "best": 0})
        quote = r.get("contradicting_disclosure")
        row["evidence"].append({"quote": quote,
                                "narrative": r.get("narrative"),
                                "why": r.get("why_it_matters"),
                                "confidence": r.get("confidence"),
                                "score": _specificity(quote),
                                "doc": Path(t["file"]).name, "model": t.get("model"),
                                # bench.py-058: which question produced this row, and when —
                                # tasks written before this field existed carry channel=None.
                                "channel": t.get("channel"), "at": t.get("at")})
        row["best"] = max(row["best"], _specificity(quote))
    out = sorted(by.values(), key=lambda x: (-x["best"], -len(x["evidence"])))
    _write(BENCH, {"built": _now(), "question": question(), "rows": out,
                   "_doc": "Ranked by a coded specificity score (numeric figure + a change-to-"
                           "prior-arrangement verb in the quote), not model confidence and not "
                           "a multiple — see bench.py-030. A row is a LEAD; the full evidence "
                           "gate is unchanged before any order. Each evidence item carries the "
                           "channel and UTC time it was extracted (bench.py-058)."})
    funnel_record("bench:ranked", n_matched, len(out))
    print(f"bench.json: {len(out)} names with verbatim-verified evidence")
    for r in out[:10]:
        print(f"  {r['best']}/10 {r['ticker']:<6} {len(r['evidence'])} quote(s) · "
              f"{(r['evidence'][0]['quote'] or '')[:80]}")
    return out


def _channel_stats(channel):
    """bench.py-058: claimed/survived totals for every task DONE under this exact channel,
    across the whole durable queue — the brief's "what did THIS question actually produce"
    line. A task written before channel-stamping shipped has channel=None and never matches.
    bench.py-176: matches on _chan_id (the leading token), not the full string, so a
    wording edit to the channel description doesn't orphan every task stamped before it."""
    q = _load()
    key = _chan_id(channel)
    done = [t for t in q["tasks"].values() if t.get("state") == "done" and _chan_id(t.get("channel")) == key]
    claimed = sum(1 for t in done if t.get("claimed"))
    survived = sum(1 for t in done if t.get("survived"))
    return {"read": len(done), "claimed": claimed, "survived": survived}


def _corpus_forms():
    """bench.py-059: what SEC forms the corpus actually contains, counted from the files on
    disk — e.g. {'10-K': 2252, '10-Q': 2254}. A channel asking about a mechanism disclosed
    in forms absent here (S-1/S-3/424B/8-K/25-NSE — lock-ups, shelves, delistings) cannot
    find it no matter how good the question is; this is the provenance that says so up front."""
    from collections import Counter
    c = Counter()
    if not CORPUS.exists():
        return {}
    for d in CORPUS.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.txt"):
            m = re.search(r"_([A-Za-z0-9/-]+)\.txt$", f.name)
            if m:
                c[m.group(1)] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))



ALIAS_LISTING_TYPES = {"CDI", "ADR", "GDR"}


def _alias_listing_type(tk):
    """bench.py-027: AVHHL's card carried AVITA Medical's ASX depositary-interest price
    ($1.42) against RCEL's own XBRL-derived share count, a ~7x market-cap error, because
    Stage A's universe() picks one ticker per CIK from company_tickers.json and CIKs with
    a depositary-receipt listing alongside their primary one can lose that pick to the DR.

    The PM's proposed test — skip if Finnhub /stock/profile2?symbol=X resolves to a
    DIFFERENT ticker — does NOT discriminate: verified empirically against all 6 known
    non-primary-ticker cards, profile2 resolves ALL SIX (including the 5 genuine distinct
    securities: ATROB, FCELB, HSCSW, LBRDP, NCRRP) to a different symbol. Shipping that
    rule would have skipped cards for 5 real securities to catch 1 true alias.

    Finnhub's /search 'type' field is narrower and DOES separate them on the same 6-name
    check: AVHHL -> 'CDI' (a CHESS Depositary Interest — by definition a duplicate of a
    primary listing elsewhere, never itself an XBRL-reporting entity); the other 5 came
    back 'Common Stock' / 'Equity WRT' / 'PUBLIC' / ''. ADR/GDR are the same duplicate-
    listing shape as CDI and get the same treatment on the same reasoning, though only
    CDI is verified against a live case."""
    try:
        key = json.loads((ENGINE / "config" / "keys.json").read_text()).get("finnhub", "")
        with urllib.request.urlopen(
                f"https://finnhub.io/api/v1/search?q={tk}&token={key}", timeout=15) as r:
            d = json.loads(r.read())
        for row in d.get("result") or []:
            if (row.get("symbol") or "").upper() == tk.upper():
                return row.get("type") or ""
    except Exception:
        pass
    return ""


def cards(limit=15):
    """Build a fincard for the top bench leads that do not have one.

    The Bench reads the whole market but the number pipeline only ever covered the ~49
    names we had researched, so a lead arrived on the desk as a ticker and a quote with no
    scale attached — the PM had to leave the page to find out whether it was a mid-cap or a
    large-cap company. Stage A already knows revenue; everything else (market cap, net cash,
    EV/FCF, P/B) needs one companyfacts fetch per name, which is ~6s.

    Bounded on purpose: the top `limit` UNHELD, UNCARDED leads only. Building 85 cards a
    night to service a list nobody reads past the top ten is the kind of thoroughness that
    is really just cost."""
    import subprocess
    b = _j(BENCH, {})
    rows = b.get("rows") or []
    todo = []
    for r in rows:
        tk = r["ticker"].upper()
        if (NAMES / tk / "fincard.json").exists():
            continue
        if not tk.isalpha():          # class-share tickers (FCELB) are not fetchable this way
            continue
        todo.append(tk)
        if len(todo) >= limit:
            break
    if not todo:
        funnel_record("bench:cards", 0, 0)
        print("bench cards: every top lead already has a card")
        return []
    built, failed, skipped = [], [], []
    for tk in todo:
        alias = _alias_listing_type(tk)
        if alias in ALIAS_LISTING_TYPES:
            skipped.append(f"{tk}({alias})")
            continue
        (NAMES / tk).mkdir(parents=True, exist_ok=True)
        try:
            p = subprocess.run([str(ENGINE / ".venv/bin/python"), str(ENGINE / "valuation/fincard.py"),
                                tk, "--out", str(NAMES / tk / "fincard.json")],
                               capture_output=True, text=True, timeout=180)
            if p.returncode == 0:
                built.append(tk)
            else:
                # bench.py-156: a bare ticker list told the reader nothing was wrong
                # without saying what — the last non-empty stderr line (fincard.py's
                # SystemExit/exception text) or, failing that, the exit code.
                lines = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
                reason = lines[-1][:150] if lines else f"exit {p.returncode}, no stderr"
                failed.append((tk, reason))
        except subprocess.TimeoutExpired:
            failed.append((tk, "timed out after 180s"))
        except Exception as e:
            failed.append((tk, f"{type(e).__name__}: {str(e)[:150]}"))
    funnel_record("bench:cards", len(todo), len(built))
    print(f"bench cards: {len(built)} built, {len(failed)} failed"
          + (f" ({'; '.join(f'{tk}: {reason}' for tk, reason in failed)})" if failed else "")
          + (f", {len(skipped)} skipped as depositary-listing aliases ({', '.join(skipped)})"
             if skipped else ""))
    return built


def _fmt_m(v):
    """v in dollars -> '$-931.3M' etc.; None/missing -> 'n/a'."""
    if v is None:
        return "n/a"
    return f"${v / 1_000_000:,.1f}M"


def _fincard_line(tk):
    """bench.py-097: net_cash, fcf, ev_over_fcf, market_cap (+ card build date, flag count)
    from names/<TK>/fincard.json, so the balance-sheet kill happens reading the brief instead
    of the PM re-deriving it with an ad-hoc loop over fincard.json every morning (80% of
    channel-13 leads died on that pass on 2026-09-01/02 alone). ev_over_fcf isn't a fincard
    field (fincard.py is numbers-owned) — computed here from its own enterprise_value/fcf."""
    f = NAMES / tk / "fincard.json"
    if not f.exists():
        return None
    c = _j(f, {})
    d = c.get("derived") or {}

    def v(key):
        row = d.get(key)
        return row.get("value") if isinstance(row, dict) else None

    net_cash, fcf, ev, mcap = v("net_cash"), v("fcf"), v("enterprise_value"), v("market_cap")
    ev_over_fcf = "n/m" if not fcf else f"{ev / fcf:.1f}x" if ev is not None else "n/a"
    flags = c.get("flags") or []
    built = (c.get("built") or "?")[:10]
    return (f"- **Balance sheet** (`fincard` built {built}, {len(flags)} flag(s)): "
            f"net cash {_fmt_m(net_cash)} · FCF {_fmt_m(fcf)} · EV/FCF {ev_over_fcf} · "
            f"mkt cap {_fmt_m(mcap)}")


_DOLLAR_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?\b", re.I)

def _backlog_dollar(quote):
    """bench.py-181/found building the 09-08 new-names table: the FIRST dollar figure
    inside a quote, converted to plain dollars — not the largest. A 10-K/10-Q backlog
    sentence that states more than one period ("as of DATE1 and DATE2, was $A and $B")
    always lists the CURRENT period first, so the first figure is the current one and a
    later figure is a prior-period comparison. Picking the largest instead is wrong in
    both directions found live: ICFI's 'backlog was $3,405.0M, $3,786.3M, and $3,777.8M
    at Dec 31 2025, 2024, and 2023' has 2024's figure as the largest, not 2025's current
    one; HOLOW's 'as of Mar 31 2023 and Dec 31 2022... is $0 and $384,489' has the PRIOR
    period's $384,489 as the largest, against a current figure of $0 — 'largest' turned
    a near-zero backlog into the #1-ranked lead in the whole table. An explicit unit word
    is trusted; a bare figure under $10M with no unit is a filing-table convention (the
    table header says 'in thousands', the cell doesn't repeat it) — assumed as thousands
    and flagged, since treating it as literal dollars would read a real backlog like
    GHM's $532,637(thousand) as $532,637. Returns (dollars, assumed) or None if the quote
    carries no dollar figure at all."""
    m = _DOLLAR_RE.search(quote or "")
    if not m:
        return None
    raw = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit == "billion":
        return raw * 1e9, False
    if unit == "million":
        return raw * 1e6, False
    if unit == "thousand":
        return raw * 1e3, False
    if raw < 10_000_000:
        return raw * 1e3, True
    return raw, False


def _backlog_to_cap(r):
    """bench.py-181, night 4 of channel 11: every one of 46 names printed 6/10 — the coded
    specificity score saturated and stopped separating anything (bench.py-030 again). The
    backlog dollar figure over market cap does separate them (CNDT 4.31x, PRIM 1.50x, CVU
    1.46x per STRATEGY-PROPOSAL-v3 §1's examples). Returns (dollars, assumed, ratio) where
    ratio is None if there's no fincard market_cap to divide by — the caller prints 'no cap'
    rather than dropping the name, since a missing cap is a data gap, not disqualifying."""
    best = best_ev = None
    for e in r.get("evidence") or []:
        got = _backlog_dollar(e.get("quote"))
        if got and (best is None or got[0] > best[0]):
            best, best_ev = got, e
    if best is None:
        return None
    dollars, assumed = best
    mcap = ((_j(NAMES / r["ticker"].upper() / "fincard.json", {}).get("derived") or {})
            .get("market_cap") or {}).get("value")
    return (dollars, assumed, mcap, dollars / mcap if mcap else None, best_ev)


def brief(top=25):
    """bench_brief.md — the overnight read, written up for the PM's desk.

    The bench used to end at bench.json and no one read it: the PM's desk list never named
    the file, and the PM's own session log called the bench something it had "not run" —
    treating a staff member who had already worked all night as a tool it had declined to
    use. This turns the night's output into a brief that arrives on the desk like any other.

    Names the PM already owns or already passed on are marked, so the read is about what is
    NEW rather than a list it has to re-triage every morning."""
    b = _j(BENCH, {})
    rows = b.get("rows") or []
    held, uni, seen = set(), set(), {}
    try:
        pf = _j(DATA / "portfolio.json", {})
        held = {p.get("symbol") for p in pf.get("positions", []) if p.get("symbol")}
    except Exception:
        pass
    try:
        uni = {ln.split()[0].strip().upper() for ln in (HERE / "universe.txt").read_text().splitlines()
               if ln.strip() and not ln.strip().startswith("#") and ln.split()}
    except Exception:
        pass
    try:
        cj = _j(DATA / "candidates.json", {})
        for v in (cj.get("items") or {}).values():
            tk = (v.get("ticker") or "").upper()
            if tk and v.get("status") in ("pm_reviewed", "dropped", "underwriting"):
                seen[tk] = v.get("status")
    except Exception:
        pass

    fresh = [r for r in rows if r["ticker"].upper() not in held
             and r["ticker"].upper() not in seen
             and r["ticker"].upper() not in uni]  # bench.py-176: uni was computed, never applied
    qn = b.get("question") or {}
    cur_channel = qn.get("channel")
    stats = _channel_stats(cur_channel)
    # bench.py-181: channel 11's coded specificity score saturated (every name 6/10) and
    # stopped separating anything; backlog-dollar / market-cap does. Scoped to channel 11
    # only — the ratio is meaningless for the other channels' questions.
    btc = {}
    if _chan_id(cur_channel) == "11":
        btc = {r["ticker"].upper(): _backlog_to_cap(r) for r in fresh}
        def _btc_key(r):
            got = btc.get(r["ticker"].upper())
            if not got:
                return (2, 0.0)          # no $ figure in evidence at all
            if got[3] is None:
                return (1, 0.0)          # $ figure found, but no fincard cap to divide by
            return (0, -got[3])          # ranked by ratio, largest first
        fresh.sort(key=_btc_key)
    forms = _corpus_forms()
    forms_line = ", ".join(f"{n} {f}" for f, n in forms.items()) or "corpus not built yet"
    # bench.py-058/-072: what THIS channel actually produced, first line, unmissable — a run
    # that claimed 0 must say so before any evidence rows, not bury it under yesterday's
    # leads. rank() now only ever writes bench.json rows whose evidence is from cur_channel
    # (bench.py-072 — it used to blend in every earlier question's surviving reads with no
    # per-row flag), so every row below IS this channel's; nothing here is carried over.
    L = [f"# The Bench — overnight read, built {b.get('built', '?')}", "",
         f"**This channel ({cur_channel or '?'}) read {stats['read']} filings this run · "
         f"claimed {stats['claimed']} · {stats['survived']} SURVIVED the verbatim check.**",
         "", f"**Corpus:** {forms_line} (bench.py-059 — a channel asking about a mechanism "
             "disclosed in forms outside this mix will find nothing here regardless of the "
             "question's quality).", "",
         "_Your local analyst read primary filings across the market all night, at zero Claude",
         "token cost. Ranked by a CODED score (a concrete figure + a change-to-prior-arrangement",
         "verb in the quote), not the model's own confidence — that field saturated at 9/10 across",
         "the corpus and stopped separating anything (bench.py-030) — and never by a multiple.",
         "Every row below is a **LEAD**: the full evidence gate is unchanged before any dollar",
         "moves. A quote here is verbatim-verified against the filing named; the characterisation",
         "next to it is the local model's and is NOT._", "",
         f"**Question asked:** {qn.get('channel', '?')} "
         f"(set by {qn.get('set_by', '?')}{' on ' + qn['set_at'] if qn.get('set_at') else ''})", "",
         f"**{len(rows)} names carry evidence from THIS channel · {len(fresh)} are new to you** "
         f"({len(held & {r['ticker'].upper() for r in rows})} already held, "
         f"{len(seen.keys() & {r['ticker'].upper() for r in rows})} already triaged in the funnel, "
         f"{len(uni & {r['ticker'].upper() for r in rows} - held - seen.keys())} already on your "
         f"watchlist).",
         ""]
    if not rows:
        L += ["_No evidence rows — either the queue was empty or every read failed the verbatim",
              "check. Check `_engine/logs/bench.log` before treating this as a quiet market._"]
    L += [f"## New to you — ranked by {'backlog/cap' if btc else 'evidence'}", ""]
    for r in fresh[:top]:
        got = btc.get(r["ticker"].upper()) if btc else None
        # signals-192: the displayed quote used to be evidence[0] unconditionally, while the
        # backlog/cap ratio below it was computed from whichever evidence carried the LARGEST
        # dollar figure (_backlog_to_cap) -- on a multi-quote row (e.g. ULBI: a $706 warranty-
        # deferred-revenue quote at evidence[0] next to a $117.5M backlog quote at evidence[1])
        # the brief showed the small, low-score quote right above a ratio computed from the
        # other one entirely. Show the SAME evidence the ratio came from whenever there is one.
        e = (got[4] if got else None) or (r.get("evidence") or [{}])[0]
        L += [f"### {r['ticker']} — {r.get('best', '?')}/10 · {len(r.get('evidence', []))} quote(s)",
              f"- **The story it contradicts:** {e.get('narrative') or '—'}",
              f"- **Why it matters:** {e.get('why') or '—'}",
              f"- **Verbatim, from `{e.get('doc') or '?'}`:** > {(e.get('quote') or '—')[:600]}"]
        if btc:
            if not got:
                L.append("- **Backlog/cap:** no $ figure in the quoted evidence")
            else:
                dollars, assumed, mcap, ratio, _ = got
                amt = f"${dollars/1e6:,.1f}M" + (" (assumed $K table)" if assumed else "")
                L.append(f"- **Backlog/cap:** {amt} / {_fmt_m(mcap)} cap = "
                          + (f"**{ratio:.2f}x**" if ratio is not None else "no cap"))
        fc = _fincard_line(r["ticker"].upper())
        if fc:
            L.append(fc)
        L.append("")
    if len(fresh) > top:
        L.append(f"_…and {len(fresh) - top} more in `data/bench.json`._\n")
    def _status(tk):
        if tk in held:
            return "held"
        if tk in seen:
            return seen[tk]
        return "watched"  # bench.py-176: on universe.txt but not held/triaged
    already = [r for r in rows if r["ticker"].upper() in held or r["ticker"].upper() in seen
               or r["ticker"].upper() in uni]
    if already:
        L += ["## Already on your book, triaged, or watched", "",
              ", ".join(f"**{r['ticker']}** ({_status(r['ticker'].upper())})"
                        for r in already[:40]), ""]
    BENCH_BRIEF.write_text("\n".join(L) + _stamp())
    funnel_record("bench:new-to-PM", len(rows), len(fresh))
    print(f"bench_brief.md: {len(rows)} names, {len(fresh)} new to the PM")
    return BENCH_BRIEF


if __name__ == "__main__":
    a = sys.argv[1:2]
    def arg(flag, d=None, cast=str):
        return cast(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else d
    if a == ["universe"]:
        universe()
    elif a == ["fetch"]:
        fetch(arg("--limit", 120, int))
    elif a == ["fill"]:
        fill(arg("--limit", 40, int))
    elif a == ["work"]:
        work(arg("--minutes", 60, int), arg("--model", "fast"), arg("--worker"),
             think=(True if "--think" in sys.argv else (False if "--no-think" in sys.argv else None)))
    elif a == ["status"]:
        status()
    elif a == ["rank"]:
        rank()
        cards(arg("--limit", 15, int))   # price the top leads before writing them up
        brief()   # the night ends on the PM's desk, not in a JSON file
    elif a == ["cards"]:
        cards(arg("--limit", 15, int))
    elif a == ["brief"]:
        brief()
    elif a == ["question"]:
        # bench.py-012: a bare positional used to set ONLY 'ask', leaving 'rule' and
        # 'channel' stuck on DEFAULT_QUESTION's — e.g. rewriting the ask to stop asking
        # for a narrative while the inherited rule still policed a narrative quote.
        # --ask/--rule/--channel/--schema let the PM replace exactly the fields it means
        # to; the positional form is kept as shorthand for --ask only.
        q = question()
        positional = sys.argv[2] if sys.argv[2:] and not sys.argv[2].startswith("--") else None
        for flag, key in (("--ask", "ask"), ("--rule", "rule"),
                          ("--channel", "channel"), ("--schema", "schema"),
                          ("--claim-key", "claim_key")):
            v = arg(flag)
            if v is not None:
                q[key] = v
        if positional is not None:
            q["ask"] = positional
        # bench.py-066: the gate reads q['claim_key'] — REFUSE any question whose
        # schema does not declare that boolean, so a renamed key can never re-arm
        # the silent-zero failure (channels 12/13 scored 78 TRUE reads as 0).
        bools = re.findall(r'"(\w+)":\s*true\|false', q.get("schema") or "")
        ck = arg("--claim-key") or q.get("claim_key")
        if ck not in bools:
            if len(bools) == 1:
                ck = bools[0]
            else:
                sys.exit(f"REFUSED: schema booleans {bools} do not include claim_key "
                         f"{ck!r} — pass --claim-key naming the boolean the gate should read")
        q["claim_key"] = ck
        q["set_by"] = "PM"; q["set_at"] = _now()
        _write(QFILE, q)
        print(f"question set: channel={q['channel']!r} ask={q['ask'][:60]!r}... rule={q['rule'][:60]!r}...")
        forms = _corpus_forms()
        # bench.py-059: printed at set-time so the PM sees the constraint BEFORE spending a
        # night on a channel the corpus structurally cannot answer (e.g. lock-ups/shelves,
        # which live in S-1/S-3/424B/8-K — forms this 10-K/10-Q corpus does not contain).
        print("corpus forms: " + (", ".join(f"{n} {f}" for f, n in forms.items()) or "not built yet"))
    else:
        sys.exit(__doc__)
