#!/usr/bin/env python3
"""
Numbers watchdog — a local model + code sweeping every number, nightly.

WHY (David, 2026-08-13: "i still want a local model going through the numbers…
have it help wherever it can"). All three real errors (LBRDP term, TRIP $824M,
LYFT net-cash) lived in PROSE — numbers asserted in memos that no code path
ever touched. Three layers, cheapest first:

  A  CODE — internal consistency of every fincard against itself:
     balance identity (assets ≈ liabilities + equity), gross profit ≈
     revenue − cogs, TTM figure == sum of its own quarter series, and every
     derived value recomputed independently from the card's figures.
  B  LOCAL vs FILINGS — extended fincheck: qwen reads the printed statements
     (latest 10-Q AND 10-K) for 8 figures; code compares scale- and
     period-aware (via the card's own quarter sums).
  C  LOCAL vs PROSE — qwen extracts every dollar figure from the PM's ACTIVE
     prose (BOOK.md, the newest memo per held name, thesis.json notes); code
     then requires each number to trace to a source: a fincard figure/derived/
     series value (±2.5%, any scale) or a verbatim hit in the filing text.
     Unsourced numbers become quality-queue items — the PM or fixer must
     source or fix them. Prices/personal position math are exempt (broker
     territory, not filing territory).

Zero Claude tokens. Findings land in names/<TK>/numcheck.json + data/
numwatch.json; quality.py picks them up. Cron: nightly after refresh_cards.
CLI: numwatch.py run | numwatch.py memo <path> [TICKER]
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
NAMES = HERE / "names"
JOURNAL = HERE / "journal"
sys.path.insert(0, os.path.expanduser("~/maintenance/bin"))
from localllm import ask_json  # noqa: E402

TOL = 0.025


def _j(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


# ---------------- A: code-only card self-consistency ----------------
def check_card(card):
    finds = []
    F, D, S = card.get("figures", {}), card.get("derived", {}), card.get("series", {})

    def gv(k):
        f = F.get(k) or {}
        if f.get("STALE"):
            return None   # quarantined by fincard — derived excluded it; recompute must too
        return f.get("value")

    def close(a, b, tol=TOL):
        return a is not None and b is not None and abs(a - b) <= tol * max(abs(a), abs(b), 1)

    ta, tl, eq = gv("total_assets"), gv("total_liabilities"), gv("equity")
    if ta is not None and tl is not None and eq is not None and not close(ta, tl + eq, 0.03):
        finds.append(f"balance identity: assets {ta:,.0f} != liabilities {tl:,.0f} + equity {eq:,.0f} "
                     f"(gap {(ta - tl - eq) / ta * 100:+.1f}% — mixed dates or minority-interest tag)")
    gp, rev, cogs = gv("gross_profit"), gv("revenue"), gv("cogs")
    if gp is not None and rev is not None and cogs is not None and not close(gp, rev - cogs, 0.03):
        finds.append(f"gross_profit {gp:,.0f} != revenue - cogs {rev - cogs:,.0f}")
    for k, fig in F.items():
        qs = (S.get(k) or {}).get("quarters", [])
        if len(qs) >= 4 and str(fig.get("period", "")).startswith("TTM"):
            ssum = sum(q["value"] for q in qs[:4])
            if not close(fig.get("value"), ssum, 0.001):
                finds.append(f"{k}: TTM figure {fig.get('value'):,.0f} != sum of own quarters {ssum:,.0f}")
    # independent recompute of key deriveds from raw figures
    cash, sti = gv("cash") or 0, gv("st_investments") or 0
    dlt, dcur = gv("debt_lt") or 0, gv("debt_current") or 0
    nc = (D.get("net_cash") or {}).get("value")
    if nc is not None and not close(nc, cash + sti - dlt - dcur, 0.001):
        finds.append(f"net_cash derived {nc:,.0f} != recompute {cash + sti - dlt - dcur:,.0f}")
    fcf = (D.get("fcf") or {}).get("value")
    cfo, capex = gv("cfo"), gv("capex")
    if fcf is not None and cfo is not None and not close(fcf, cfo - (capex or 0), 0.001):
        finds.append(f"fcf derived {fcf:,.0f} != CFO - capex recompute {cfo - (capex or 0):,.0f}")
    if fcf is not None and capex is None:
        finds.append("fcf computed with capex MISSING — value is CFO (upper bound); "
                     "any prose citing it as FCF is suspect")
    return finds


# ---------------- C: prose-number tracing ----------------
NUM_RE = re.compile(r"\$?\s?\d[\d,]*\.?\d*\s?(?:billion|million|thousand|[BMK])?", re.I)


def _absolute(num_text):
    m = re.search(r"([\d,]+\.?\d*)", num_text)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    low = num_text.lower()
    if "b" in low or "billion" in low:
        v *= 1e9
    elif "m" in low or "million" in low:
        v *= 1e6
    elif "k" in low or "thousand" in low:
        v *= 1e3
    return v


def extract_prose_numbers(text, label):
    """qwen parses prose into structured dollar-figure claims; code does ALL comparison."""
    v = ask_json(
        "Extract every DOLLAR AMOUNT that describes a COMPANY's finances (cash, debt, revenue, "
        "FCF, EBITDA, market cap, buybacks, distributions, valuations) from this trading memo "
        "excerpt. SKIP: share prices, per-share values under $100, the trader's own position "
        "sizes/P&L (a cost basis, a share-of-book percentage), dates, percentages. For each: the amount "
        "EXACTLY as written and a 2-4 word label of what it claims to be. JSON "
        "{\"numbers\":[{\"text\":\"$824.4M\",\"label\":\"omitted borrowings\"}]}  Max 20.\n\n"
        + text[:11000], num_predict=900)
    out = []
    for n in (v.get("numbers") or []) if isinstance(v, dict) else []:
        a = _absolute(str(n.get("text", "")))
        if a and a >= 5e5:      # ignore sub-$500k noise
            out.append({"text": n["text"], "label": str(n.get("label", ""))[:40],
                        "abs": a, "source_doc": label})
    return out


# label keyword -> the ONLY card concepts that label may match. Unconstrained
# matching against ~200 card values produced pure coincidences ($2B "net cash"
# traced to working_capital; $1.1B "TTM FCF" to sga) — autopsy 2026-08-13.
FAMILIES = [
    (("net cash",), ["net_cash"]),
    (("gross cash", "cash and investments", "cash & investments", "total cash"),
     ["cash", "st_investments", "lt_investments", "_cash_combo"]),
    # free-cash/operating-cash BEFORE the bare "cash" entry below: matching is substring-
    # based, and "cash" is a substring of "operating cash flow" and "free cash flow" —
    # checking the generic entry first swallowed both into the wrong family and made
    # H1/9mo cfo figures (which the bare "cash" family has no series data to source,
    # since cash is a balance-sheet point-in-time, not a flow with quarters to sum)
    # permanently unsourceable. TLS's own "H1-2026 operating cash flow $17,489K" was
    # matched against cash/st_investments/net_cash instead of cfo (2026-08-18).
    (("free cash", "fcf"), ["fcf", "cfo"]),
    (("operating cash", "cfo"), ["cfo"]),
    # sbc before the bare "cash" entry too: "non-cash SBC" contains "cash" as a whole
    # word (inside "non-cash"), which would otherwise win by list order even though
    # the label is explicitly describing SBC, not a cash balance (2026-08-18).
    (("sbc", "stock-based comp", "stock based comp", "share-based comp"), ["sbc"]),
    (("cash",), ["cash", "st_investments", "_cash_combo", "net_cash"]),
    (("ebitda",), ["ebitda_approx"]),
    (("net income", "earnings", "profit"), ["net_income"]),
    (("revenue", "sales"), ["revenue"]),   # NOT "bookings" — Gross Bookings is a KPI, not GAAP revenue
    # "net debt" BEFORE the bare "debt" entry below: word-boundary matching finds "debt"
    # inside "net debt" too, and the generic debt family checks GROSS debt concepts —
    # MBGL's memo/thesis "$1,795M (net debt)" is the card's net_cash, negated, to the
    # dollar (net_cash -1,795,000,000). abs() comparison already makes the match
    # sign-blind; net debt just needed to reach net_cash before "debt" swallowed it
    # (2026-08-19).
    (("net debt",), ["net_cash"]),
    (("debt", "borrowings", "notes"), ["total_debt", "debt_lt", "debt_current"]),
    (("market cap", "capitalization"), ["market_cap"]),
    (("enterprise value", "ev"), ["enterprise_value"]),
    (("equity", "book value"), ["equity"]),
    (("buyback", "repurchase"), ["buybacks"]),
    (("dividend", "distribution"), ["dividends_paid"]),
    (("capex", "capital expenditure"), ["capex"]),
    (("asset",), ["total_assets"]),
]
# A number the PM labels as WRONG is the PM documenting a defect it found — flagging it as
# unsourced inverts the meaning. ARI's thesis notes carry "erroneous cash figure $758.685M"
# and "dividend calculation error $480.795M": both are the PM recording an error, and both
# were reported back at it as errors.
ERROR_WORDS = ("erroneous", "error", "incorrect", "misstated", "mistaken", "wrong",
               "typo", "overstated", "understated", "corrected", "correction")

# The PM's own arithmetic. "implied REO asset value", "my base case" — derived numbers that
# are SUPPOSED to be absent from the filings; that is what makes them the PM's variant view
# rather than a quote. They still must be re-derivable, which is the memo audit's job, not
# the watchdog's.
MODEL_WORDS = ("implied", "modelled", "modeled", "my ", "derived", "scenario", "assumes",
               "assumed", "bear case", "base case", "bull case", "back-of", "reverse dcf",
               "sotp", "rnpv", "haircut", "stress", "stressed", "levered")

FORWARD_WORDS = ("guide", "guidance", "target", "estimate", "expected", "forecast",
                 "consensus", "e)", "fy26e", "fy27e", "projected", "trim")


def _rolling_sums(quarters, n):
    """Every contiguous n-quarter window sum, e.g. n=2 -> every H1/H2-shaped figure,
    n=3 -> every 9-month YTD-shaped figure — a PM citing 'H1-2025 operating cash flow'
    is quoting the filing's own half-year column, which is real filed data (Q1+Q2
    summed) that just never had a single card figure/series point representing it
    directly. Confirmed against TLS's own 10-Q 2026-08-18: H1-2026 cfo $17,489K =
    Q1'26 $8,656K + Q2'26 $8,833K to the dollar; H1-2025 $13,056K = Q1'25 $6,106K +
    Q2'25 $6,950K likewise — six TLS "UNSOURCED" findings were this exact gap."""
    qs = quarters[:8]
    out = []
    for i in range(len(qs) - n + 1):
        window = qs[i:i + n]
        if all(isinstance(q.get("value"), (int, float)) for q in window):
            out.append(sum(q["value"] for q in window))
    return out


def _card_values(card, keys):
    """All values the given concept keys can honestly produce (incl. cash combos)."""
    F, D, S = card.get("figures", {}), card.get("derived", {}), card.get("series", {})
    out = []
    for k in keys:
        if k == "_cash_combo":
            c = (F.get("cash") or {}).get("value") or 0
            s = (F.get("st_investments") or {}).get("value") or 0
            l = (F.get("lt_investments") or {}).get("value") or 0
            if c:
                out += [("cash+st_investments", c + s), ("cash+st+lt_investments", c + s + l)]
            continue
        for src, blob in (("figure", F), ("derived", D)):
            v = (blob.get(k) or {}).get("value")
            if isinstance(v, (int, float)):
                out.append((f"{src}:{k}", v))
        # INSTANT concepts (cash, debt, equity...) store history as `points`; only FLOW
        # concepts use quarters/annual. Reading one and not the other meant a correctly
        # cited PRIOR-PERIOD balance — "prior year debt $1,224,759K" on ARI — could never
        # match anything and was reported as unsourced.
        for pt in ((S.get(k) or {}).get("points") or [])[-12:]:
            v = pt.get("value")
            if isinstance(v, (int, float)):
                out.append((f"series:{k}@{pt.get('asof', '?')}", v))
        qs = (S.get(k) or {}).get("quarters") or []
        for q in qs[:8] + ((S.get(k) or {}).get("annual") or [])[:4]:
            out.append((f"series:{k}", q.get("value")))
        for n in (2, 3):
            for v in _rolling_sums(qs, n):
                out.append((f"series:{k}:{n}q-sum", v))
    if "fcf" in keys and "cfo" in keys:
        cfo_qs = (S.get("cfo") or {}).get("quarters") or []
        capex_qs = (S.get("capex") or {}).get("quarters") or []
        capex_by_end = {q["end"]: q["value"] for q in capex_qs if isinstance(q.get("value"), (int, float))}
        for n in (1, 2, 3):
            for i in range(len(cfo_qs[:8]) - n + 1):
                window = cfo_qs[i:i + n]
                if all(w["end"] in capex_by_end for w in window):
                    out.append((f"series:fcf:{n}q-sum",
                               sum(w["value"] for w in window) - sum(capex_by_end[w["end"]] for w in window)))
    return [(s, v) for s, v in out if isinstance(v, (int, float)) and v]


def _in_filings_near(a, label, filing_texts, window=600):
    """Is this number printed in a filing NEAR the words its label uses?

    The bare-digit check demands >=3 significant digits, because "6.5" on its own matches
    almost any document. That guard is right, and it also made every small segment or
    property figure permanently unverifiable — "$6.5M Brooklyn revenue", "$6.1M Atlanta
    revenue", "$30M segment operating profit". Those were the entire residue of the
    watchdog after every other fix: 43 findings, nearly all of them 2-significant-digit
    figures that are real, quotable, and printed in the 10-Q.

    Context restores the strength the digits lack. "Brooklyn" is distinctive; "6.5" within
    600 characters of it is not a coincidence. This is a STRONGER test than the bare digit
    match, not a looser one — it requires the number AND its subject to co-occur."""
    stop = {"the", "of", "and", "for", "in", "at", "to", "a", "q1", "q2", "q3", "q4", "h1",
            "h2", "ttm", "fy", "revenue", "expenses", "income", "profit", "cash", "flow",
            "total", "net", "old", "new", "figure", "value", "cost", "costs", "capex",
            "buybacks", "repurchases", "segment", "operating"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", label) if w.lower() not in stop]
    if not words:
        return None
    pats = []
    for scale in (1e6, 1e3, 1):
        v = a / scale
        if 0.01 <= v < 1e6:
            for dec in (0, 1, 2):
                pats.append(f"{v:,.{dec}f}".rstrip("0").rstrip(".") if dec else f"{v:,.0f}")
    # >=2 significant digits even WITH context. The first version emitted "6" as a pattern
    # for $6.5M and duly "verified" Brooklyn revenue against the digit 6, which is not
    # verification, it is laundering. Context buys one digit of slack against the bare-digit
    # rule's three; it does not buy a match on a single character.
    pats = [p for p in dict.fromkeys(pats)
            if p and len(re.sub(r"[^1-9]", "", p)) >= 2]
    for name, txt in filing_texts.items():
        low = txt.lower()
        for w in words:
            start = 0
            wl = w.lower()
            while True:
                i = low.find(wl, start)
                if i < 0:
                    break
                seg = txt[max(0, i - window): i + window]
                segn = seg.replace(",", "")
                for pat in pats:
                    if pat in seg or pat.replace(",", "") in segn:
                        return f"filing:{name}~'{w}'+{pat}"
                start = i + len(wl)
    return None


def _in_filings(a, filing_texts):
    """(see below) — also matches the WORDS form, because filings write "$8.6 billion"
    rather than "8,600" and ARI's "loan book sale price ~$8.6B" failed on exactly that."""
    """Is this number printed in one of the issuer's own filings?

    Requires >=3 significant digits, the same bar the unknown-label path already uses:
    round numbers match anything, and a false 'sourced' is worse than a false 'unsourced'."""
    # The WORDS form runs first and under its own bar. "8.6 billion" is a far more specific
    # string than the bare digits "86", so the >=3-significant-digit guard below — which is
    # right for a naked number — must not gate it. Applying that guard globally is why ARI's
    # "$8.6 billion loan book sale price" was reported as "not printed in any filing" while
    # sitting verbatim in three of its own filings, which the PM caught and I had not.
    for word, scale in (("billion", 1e9), ("million", 1e6), ("thousand", 1e3)):
        v = a / scale
        if 0.1 <= v < 1000:
            # The form must ROUND-TRIP. Rounding 8.6 to "9", or 1.23 to "1.2", and then
            # matching "9 billion" / "1.2 billion" somewhere in a filing is not
            # verification — it is the same laundering as matching $6.5M against the digit
            # "6" earlier tonight, and it let an invented 1.23e9 "verify" against a real
            # 1.2 billion. A form only counts if reading it back gives the number we
            # started with.
            forms = []
            for dec in (0, 1, 2, 3):
                w = f"{v:.{dec}f}"
                try:
                    if abs(float(w) - v) > 1e-9 * max(abs(v), 1):
                        continue
                except ValueError:
                    continue
                if len(re.sub(r"[^0-9]", "", w)) < 2:
                    continue
                forms.append(w)
                if "." in w:
                    forms.append(w.rstrip("0").rstrip("."))
            for w in dict.fromkeys(forms):
                for name, txt in filing_texts.items():
                    if f"{w} {word}" in txt.lower():
                        return f"filing:{name}~'{w} {word}'"

    pat_base = f"{a:,.0f}"
    if len(re.sub(r'[^1-9]', '', pat_base)) < 3:
        return None
    for scale in (1e6, 1e3, 1):
        scaled = a / scale
        if 0.1 <= scaled < 1e7:
            pat = f"{scaled:,.1f}".rstrip("0").rstrip(".")
            for name, txt in filing_texts.items():
                if pat in txt or pat.replace(",", "") in txt:
                    return f"filing:{name}~{pat}"
    return None


def trace_number(a, label, card, filing_texts):
    """Label-constrained tracing. Returns (status, detail):
    ok / forward (unverifiable by design) / mislabel (value exists under a
    DIFFERENT concept — the LYFT-error shape) / unsourced."""
    low = label.lower()
    if any(w in low for w in FORWARD_WORDS):
        return "forward", "forward-looking/guide — not verifiable against filings"
    if any(w in low for w in ERROR_WORDS):
        return "documented-error", "the label says this figure is WRONG — the PM recording a defect, not asserting a number"
    if any(w in low for w in MODEL_WORDS):
        return "modelled", "the PM's own derivation — absent from filings BY DESIGN; the memo audit re-derives it, not this watchdog"
    fam_keys = None
    if not any(w in low for w in ("adj", "adjusted", "non-gaap", "gross bookings", "gbv")):
        # adjusted/KPI metrics are press-release numbers — GAAP card can't confirm
        # them; they go straight to the filing-digit path below
        for words, keys in FAMILIES:
            # word-boundary, not substring: "ev" (enterprise value) is a substring of
            # "revenue" and matched every "Secure Networks revenue" label in TLS's
            # thesis notes to enterprise_value instead of revenue; "cash" is likewise
            # a substring of "operating cash flow" (fixed separately by re-ordering
            # FAMILIES, but the substring hazard is general — word-boundary it once
            # here rather than re-order around every future short keyword) (2026-08-18).
            if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
                fam_keys = keys
                break
    if fam_keys:
        # market-priced values drift with the tape after a memo is written —
        # widen tolerance instead of crying wolf on every price move
        tol = 0.15 if any(k in ("market_cap", "enterprise_value") for k in fam_keys) else TOL
        for src, v in _card_values(card, fam_keys):
            if abs(abs(v) - a) <= tol * max(abs(v), a):
                return "ok", src
        # MISLABEL verdicts only for BARE concept labels ("net cash", "ttm fcf") —
        # qualified ones (segment/brand/period: "CARFAX revenue", "Q2 FCF") legitimately
        # differ from consolidated card concepts and must not cry mislabel
        fam_words = set(w for words, ks in FAMILIES if ks == fam_keys for p in words for w in p.split())
        extras = [w for w in re.findall(r"[a-z]+", low)
                  if w not in fam_words and w not in ("ttm", "total", "current", "the", "of")]
        if not extras:
            all_keys = sorted({k for _, ks in FAMILIES for k in ks})
            for src, v in _card_values(card, all_keys):
                if abs(abs(v) - a) <= TOL * max(abs(v), a):
                    return "mislabel", (f"labeled '{label}' but the value matches {src} — "
                                        "the LYFT-error shape (wrong concept under a familiar name)")
        # BEFORE crying unsourced: is the number simply IN THE FILING, under a line the
        # card does not carry as a concept? A property-level revenue, a segment figure, a
        # note disclosure — all real, sourced, quotable numbers that no consolidated card
        # concept will ever match. This path existed only for unknown labels, so any number
        # whose label happened to resemble a card concept was declared unsourced without
        # the filings ever being read. That was 162 of 289 open rows on 2026-08-18, 83 of
        # them on names we hold, and it trained the PM to skim a list built to be read.
        hit = _in_filings(a, filing_texts) or _in_filings_near(a, label, filing_texts)
        if hit:
            return "in-filing", hit
        return "unsourced", f"no {fam_keys} value within {TOL * 100:.0f}%, and not printed in any filing"
    # unknown label family (company KPIs like Gross Bookings): filing digits, but
    # only for numbers with >=3 significant digits — round numbers match anything.
    # _in_filings_near covers the 2-sig-fig case the same way the fam_keys branch
    # above already does — non-GAAP/segment KPIs (MBGL's "$93M B2B Adjusted EBITDA")
    # are the category MOST likely to live only in a per-segment $-millions table
    # ("Adjusted EBITDA $ 272 $ 93 ...") where a bare-digit match can't clear the
    # 3-sig-fig bar but proximity to "Adjusted"/"EBITDA" still proves it (2026-08-19).
    hit = _in_filings(a, filing_texts) or _in_filings_near(a, label, filing_texts)
    if hit:
        return "ok", hit
    return "unsourced", "unknown concept and no precise filing match"


def sweep_prose(tk, texts):
    """texts: {label: prose}. Returns unsourced-number findings for one name."""
    card = _j(NAMES / tk / "fincard.json", {})
    fdir = NAMES / tk / "filings"
    filing_texts = {p.name: p.read_text(errors="replace") for p in fdir.glob("*.txt")} if fdir.exists() else {}
    finds, seen_in_filing = [], []
    for label, text in texts.items():
        for n in extract_prose_numbers(text, label):
            status, detail = trace_number(n["abs"], n["label"], card, filing_texts)
            if status in ("in-filing", "documented-error", "modelled"):
                seen_in_filing.append(f"[{status}] {n['text']} ({n['label']}) — {detail}")
                continue
            if status == "mislabel":
                finds.append(f"MISLABEL in {label}: {n['text']} ({n['label']}) — {detail}")
            elif status == "unsourced":
                finds.append(f"UNSOURCED in {label}: {n['text']} ({n['label']}) — {detail}; "
                             "source it or fix it")
    if seen_in_filing:
        import collections as _c
        kinds = _c.Counter(x[1:x.index("]")] for x in seen_in_filing)
        finds.append(f"_INFO {tk}: {len(seen_in_filing)} number(s) accounted for without being "
                     f"card concepts — " + ", ".join(f"{v} {k}" for k, v in kinds.most_common())
                     + " — sourced or by-design, not defects")
    return finds


# ---------------- orchestration ----------------
def run():
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    pf = _j(DATA / "portfolio.json", {})
    held = [p["symbol"] for p in pf.get("positions", []) if p.get("symbol")]
    thesis = {k: v for k, v in _j(DATA / "thesis.json", {}).items() if not k.startswith("_")}
    book = (JOURNAL / "BOOK.md").read_text(errors="replace") if (JOURNAL / "BOOK.md").exists() else ""
    report = {"ran_at": now, "names": {}, "n_findings": 0}
    for tk in held:
        card = _j(NAMES / tk / "fincard.json", {})
        finds = check_card(card) if card else ["no fincard on disk"]
        texts = {}
        memos = sorted(JOURNAL.glob(f"*_{tk}_*.md"))
        if memos:
            texts[memos[-1].name] = memos[-1].read_text(errors="replace")
        if tk in thesis:
            texts["thesis.json note"] = json.dumps(thesis[tk])
        # BOOK.md section for this name only (cheap targeting). Stop at the next
        # top-level bullet OR a markdown heading — without the heading stop, a
        # ticker's block runs on into whatever prose/heading follows it if that
        # prose isn't itself a "- **" bullet. TLS's block (ends after its own
        # paragraph, blank line, then "## Origination...") swept all the way through
        # a later "## Open questions" numbered list and picked up ETD's own
        # $74,378K/$120,575K figures as if they were TLS's — 6 findings that were
        # never about TLS at all (2026-08-18).
        m = re.search(rf"(?im)^- \*\*{tk}[^\n]*\n(?:(?!^- \*\*|^#).*\n)*", book)
        if m:
            texts["BOOK.md"] = m.group(0)
        finds += sweep_prose(tk, texts)
        report["names"][tk] = finds
        # _INFO rows explain numbers that turned out fine; they are recorded but are
        # not defects. Counting them as findings makes a night of real progress read
        # as no progress, which is how a metric stops being read.
        report["n_findings"] += sum(1 for f in finds if not str(f).startswith("_INFO"))
        report["n_explained"] = report.get("n_explained", 0) + sum(
            1 for f in finds if str(f).startswith("_INFO"))
        (NAMES / tk / "numcheck.json").parent.mkdir(parents=True, exist_ok=True)
        (NAMES / tk / "numcheck.json").write_text(json.dumps(
            {"ran_at": now, "findings": finds}, indent=1))
        print(f"{tk}: {len(finds)} finding(s)" + (f" — {finds[0][:90]}" if finds else ""))
    (DATA / "numwatch.json").write_text(json.dumps(report, indent=1))
    # _INFO rows explain numbers that turned out to be fine; counting them as findings makes
    # a night of real progress read as no progress, which is how a metric stops being read.
    _info = report.get("n_explained", 0)
    print(f"{now} numwatch: {report['n_findings']} finding(s) across {len(held)} names"
          + (f" (+{_info} explained away)" if _info else ""))
    return report


def memo_cmd(path, tk=None):
    p = Path(path)
    if not p.exists():
        p = JOURNAL / path
    tk = (tk or re.search(r"_([A-Z]+)_", p.name).group(1)).upper()
    finds = sweep_prose(tk, {p.name: p.read_text(errors="replace")})
    for f in finds:
        print(" ", f)
    print(f"{p.name}: {len(finds)} unsourced number(s)")
    return 1 if finds else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        run()
    elif sys.argv[1:2] == ["memo"] and len(sys.argv) > 2:
        sys.exit(memo_cmd(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    else:
        sys.exit("usage: numwatch.py run | numwatch.py memo <path> [TICKER]")
