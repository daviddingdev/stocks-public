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
    cfo, capex, capex_sw = gv("cfo"), gv("capex"), gv("capex_software")
    # capex_total mirrors fincard.py's own additive capex_software build (fincard.py-033) —
    # this recompute must use the SAME formula the card used, or every card that legitimately
    # sums PP&E + software capex false-fires here as a self-consistency break. Same span
    # guard too: a "single period on file" figure (< a full TTM/FY) is never combined.
    capex_sw_comparable = (
        "single period on file" not in ((F.get("capex") or {}).get("period") or "")
        and "single period on file" not in ((F.get("capex_software") or {}).get("period") or ""))
    if capex is not None and capex_sw and capex_sw_comparable:
        capex_total = capex + capex_sw
    elif capex is None and capex_sw:
        capex_total = capex_sw
    else:
        capex_total = capex
    if fcf is not None and cfo is not None and not close(fcf, cfo - (capex_total or 0), 0.001):
        finds.append(f"fcf derived {fcf:,.0f} != CFO - capex recompute {cfo - (capex_total or 0):,.0f}")
    if fcf is not None and capex_total is None:
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
        "EXACTLY as written and a 2-4 word label of what it claims to be. If the number is "
        "immediately followed by a parenthetical like '(anchor enterprise value)' or "
        "'(net debt)', COPY that parenthetical verbatim as the label — do not paraphrase or "
        "drop qualifying words such as anchor, net, gross, my, implied, cash, levered, "
        "stressed; those words change what the number means. JSON "
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
    # "mkt cap"/"mcap": common trading-desk abbreviations for "market cap" that don't
    # contain the phrase "market cap" as a substring, so the word-boundary match on that
    # phrase alone never fires — same abbreviation gap "ev" was added to close for
    # "enterprise value" (2026-08-18). Caught on TLS's own "$331,084k mkt cap" (matches
    # the card's market_cap 331,083,975.27 to the dollar) and MBGL's "$5,980M mcap"
    # (2026-08-20).
    (("market cap", "capitalization", "mkt cap", "mcap"), ["market_cap"]),
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
# Stems, not exact words, for the overstat/understat/misstat family (numwatch.py-055,
# 2026-08-25): "overstated" as an exact word does not match "overstatement" — TLS's
# 2026-08-18 defect writeup ("a 30% overstatement of free cash flow") kept reporting as
# UNSOURCED across two sessions even after a full correction block was appended, because
# the PM's own phrasing ("...ment" not "...ed") never matched. Substring-matched against
# low_ctx like every other entry here, so the stem alone is enough to catch every
# inflection (overstate/overstated/overstatement/overstating, etc).
ERROR_WORDS = ("erroneous", "error", "incorrect", "mistaken", "wrong",
               "typo", "overstat", "understat", "misstat", "corrected", "correction",
               "defect")

# The PM's own arithmetic. "implied REO asset value", "my base case" — derived numbers that
# are SUPPOSED to be absent from the filings; that is what makes them the PM's variant view
# rather than a quote. They still must be re-derivable, which is the memo audit's job, not
# the watchdog's.
MODEL_WORDS = ("implied", "modelled", "modeled", "my ", "derived", "scenario", "assumes",
               "assumed", "bear case", "base case", "bull case", "back-of", "reverse dcf",
               "sotp", "rnpv", "haircut", "stress", "stressed", "levered", "anchor",
               "cash interest")
# "anchor": an EV/valuation figure computed at the PM's OWN anchor price ("$9,608M
# (anchor enterprise value)") is by design not equal to the card's live enterprise_value —
# same class as "levered"/"stressed". "cash interest": the sum of stated coupon rate x
# principal across a multi-tranche debt schedule (MBGL: 650e6*.05050 + 650e6*.05450 +
# 700e6*.06050 = 110,600,000 exactly, verified against Note 4 Debt) is real and correct
# but, like every MODEL_WORDS figure, absent from the filings BY DESIGN — no filing prints
# the summed total, and no card concept holds cash-interest-paid (checked: MBGL's
# companyfacts has no InterestPaidNet or equivalent tag at all), so it can never resolve to
# anything but a false UNSOURCED/MISLABEL. numwatch-009 (2026-08-19): re-derived and
# confirmed correct by hand; the watchdog cannot re-derive a multi-tranche coupon sum any
# more than it re-derives a reverse DCF, so it is out of scope by the same logic.

FORWARD_WORDS = ("guide", "guidance", "target", "estimate", "expected", "forecast",
                 "consensus", "e)", "fy26e", "fy27e", "projected", "trim")

# a computed DELTA between two filing figures ("higher capex explains only $16.0M in the
# quarter ($29.9M across H1)") is a legitimate PM number that by construction appears in no
# single filing line — same class as MODEL_WORDS, just not a valuation derivation. LYFT's own
# H1 capex delta (50,718 - 20,786 = 29,932K) fell through to the coincidental-card-concept
# check and cried MISLABEL against a 2024 balance-sheet figure with no relationship to it
# (numwatch.py-028, 2026-08-20). Re-derivation is the memo audit's job, not this watchdog's.
DELTA_WORDS = ("delta", "swing", " vs ", "vs.", "up from", "down from", "increase", "decrease",
               "reserve build", "reserve release")
# "reserve build"/"release": standard accounting shorthand for an increase/decrease in a
# reserve balance — itself a delta by definition, but neither the word nor the figure
# carries a sign or DELTA_WORDS match above (fincard.py-060 follow-up, numbers 2026-08-27,
# LYFT: "Insurance-reserve build inside CFO is $359,793K" cried UNSOURCED against no card
# concept, even though it is an exact, verified TTM rollforward of two verbatim filing
# quotes two lines above it in the SAME memo — FY25 10-K "Insurance reserves 479,033
# 363,524 (79,482)" minus 10-Q H1'25 "127,232 246,472"'s 246,472 plus that same quote's
# 127,232 = 479,033 - 246,472 + 127,232 = 359,793 exactly, the identical FY+H1curr-H1prior
# formula the memo uses one line up for TTM CFO. Scoped to the two-word phrase, not bare
# "build" (which appears in unrelated contexts — "build a position," "build conviction").

# a delta signalled by PUNCTUATION rather than a WORD (numwatch.py-034, PM 2026-08-21):
# (1) a leading sign directly on the figure — "H1: CFO +26.6M, capex +29.9M" — the same
#     LYFT H1-capex delta DELTA_WORDS above documents, just spelled with a sign instead of
#     a word. `literal.strip().startswith(("+", "-"))` alone is not enough: the extraction
#     model's job is to copy the source "EXACTLY as written", but in practice it often
#     normalizes "+29.9M" down to "$29.9M" before this code ever sees it, dropping the
#     sign that was the whole signal. Checking CONTEXT (the raw source window) instead of
#     the model's literal survives that normalization.
# (2) either side of a "->" transition — "FCF 610.2 -> 606.9 = -0.5%" — both numbers
#     express the CHANGE together; neither is independently asserted as a filing level.
#     Confirmed as a live false MISLABEL, not just a hypothetical: 610.2 in this exact
#     LYFT memo line traced to series:buybacks:3q-sum by coincidence before this fix.
# A bare "- " at true line-start (a markdown bullet) must NOT match the sign case — the
# `[A-Za-z]` anchor requires a letter immediately before the (optionally spaced) sign, and
# a bullet dash has nothing but line-start whitespace there.
_SIGN_PREFIX_RE = re.compile(r"[A-Za-z]\s*[+\-]\s*\$?\s*$")
_ARROW_RE = re.compile(r"->|→")


def _is_punctuation_delta(context, literal):
    """True if `literal`'s occurrence in `context` is marked as a computed change by a
    leading +/- sign or a '->' transition on either side — see DELTA_WORDS comment above."""
    i = context.find(literal)
    if i < 0:
        return False
    before, after = context[:i], context[i + len(literal):]
    if _SIGN_PREFIX_RE.search(before):
        return True
    if _ARROW_RE.search(before[-6:]) or _ARROW_RE.search(after[:6]):
        return True
    return False


def _rounding_interval(num_text):
    """The dollar interval a memo's OWN precision implies: '$127.2M' asserts
    127,150,000-127,249,999, because a filing figure of 127,232 (thousands) rounds to
    127.2 exactly as printed. Returns (lo, hi) or None.

    Exact digit-string matching cannot see this: rendering 127.2e6 back out as a string
    ('127,200') will never equal the filing's own more-precise '127,232' — the memo
    correctly rounded, and exact-match punished it for doing so (numwatch.py-028,
    2026-08-20, LYFT insurance reserves)."""
    m = re.search(r"([\d,]+\.?\d*)", num_text)
    if not m:
        return None
    mantissa = m.group(1).replace(",", "")
    try:
        frac = float(mantissa)
    except ValueError:
        return None
    scale = 1.0
    low = num_text.lower()
    if "b" in low or "billion" in low:
        scale = 1e9
    elif "m" in low or "million" in low:
        scale = 1e6
    elif "k" in low or "thousand" in low:
        scale = 1e3
    v = frac * scale
    decimals = len(mantissa.split(".")[1]) if "." in mantissa else 0
    half_unit = 0.5 * scale / (10 ** decimals)
    return v - half_unit, v + half_unit


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


def _in_filings(a, filing_texts, literal=""):
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

    # ROUNDING-TOLERANT: a memo that correctly rounds a filing figure to its own stated
    # precision cannot be found by exact digit-string comparison above — the whole point
    # of rounding is that it drops digits the filing still prints. Scan the filing for any
    # printed number whose value, at any of the three common filing scales (raw dollars/
    # thousands/millions), falls inside the interval the memo's own precision implies.
    if literal:
        interval = _rounding_interval(literal)
        if interval:
            lo, hi = interval
            if hi > lo + 1:   # a meaningful rounding window, not a whole-dollar figure
                for scale in (1, 1e3, 1e6):
                    clo, chi = lo / scale, hi / scale
                    if clo <= 0:
                        continue
                    for name, txt in filing_texts.items():
                        for m in re.finditer(r"\(?-?[\d,]{3,}(?:\.\d+)?\)?", txt):
                            tok = m.group(0).strip("()")
                            try:
                                v = float(tok.lstrip("-").replace(",", ""))
                            except ValueError:
                                continue
                            if clo <= v <= chi:
                                return f"filing:{name}~'{tok}' rounds to {literal}"
    return None


def trace_number(a, label, card, filing_texts, context="", literal=""):
    """Label-constrained tracing. Returns (status, detail):
    ok / forward (unverifiable by design) / mislabel (value exists under a
    DIFFERENT concept — the LYFT-error shape) / unsourced.

    `context` is a window of the SOURCE memo text around the number's own occurrence —
    a supplement to `label`, because the 2-4 word label the extraction model produces can
    drop a qualifier that sits a clause away in the source: "anchor $26.50 -> EV $9,608M"
    puts "anchor" 12 characters before the number, never adjacent to it as a unit the
    model reliably preserves when compressing to a short label (numwatch-009, 2026-08-19).

    `literal` is the amount exactly as the source wrote it ("$127.2M") — used only for
    the rounding-tolerant filing match and the delta leading-sign check; never fed into
    the family/mislabel logic below."""
    # context is used ONLY for these three gate checks, on distinctive multi-letter
    # phrases unlikely to occur by coincidence. It must NOT reach the fam_keys/extras
    # logic below: that runs on ordinary words ("cash", "debt", "revenue"...) that a
    # numeric memo's surrounding prose will contain constantly, and would fam-match or
    # mislabel-suppress almost every number in the document if given an 80-char window.
    # whitespace-normalized: filing text wraps at arbitrary columns, so "flat at $606.9M
    # vs\n$610.2M" must still match " vs " — a literal-newline miss is what let LYFT's own
    # FCF-vs-prior-period delta cry MISLABEL instead of being recognized as one
    # (numwatch.py-028, 2026-08-20).
    low_ctx = re.sub(r"\s+", " ", (label + " " + context).lower())
    low = label.lower()
    if any(w in low_ctx for w in FORWARD_WORDS):
        return "forward", "forward-looking/guide — not verifiable against filings"
    if any(w in low_ctx for w in ERROR_WORDS):
        return "documented-error", "the label says this figure is WRONG — the PM recording a defect, not asserting a number"
    if any(w in low_ctx for w in MODEL_WORDS):
        return "modelled", "the PM's own derivation — absent from filings BY DESIGN; the memo audit re-derives it, not this watchdog"
    if (any(w in low_ctx for w in DELTA_WORDS) or literal.strip().startswith(("+", "-"))
            or _is_punctuation_delta(context, literal)):
        return "modelled", "a computed delta between two filing figures — the PM's own derivation, re-derived by the memo audit, not this watchdog"
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
        if fam_keys is None:
            # the label itself named no family ("balance sheet", not "cash") but the
            # surrounding prose did: ARI's "NOT the $1.24B on the 6/30 balance sheet"
            # sits one clause from "ARI is ~$747.0M of CASH (pro-forma...)" — a real,
            # correctly-filed figure (card cash 1,239,480,000, 0.04% off) that a
            # label-only match can never route anywhere. This still has to pass the
            # SAME tolerance check below, so a wrong guess here just falls through to
            # unsourced rather than fabricating a match — and because `low` (not
            # low_ctx) still drives the extras/mislabel check further down, a
            # context-derived match can only resolve "ok" or fall through, never
            # mislabel (numwatch-009 follow-up, 2026-08-20).
            for words, keys in FAMILIES:
                if any(re.search(rf"\b{re.escape(w)}\b", low_ctx) for w in words):
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
                    # a coincidental card-concept match is not proof of mislabeling if the
                    # number is ALSO independently printed in the filing near words from
                    # its own surrounding prose — a segment/table figure legitimately
                    # collides with an unrelated consolidated concept by chance often
                    # enough that this fired on two real, correctly-labeled, verbatim-
                    # quoted filing figures the same night: LYFT's own FCF reconciliation
                    # ("$606.9 $610.2" — matched series:buybacks:3q-sum) and MBGL's B2B
                    # segment revenue ("revenue $295M -> $313M" — matched
                    # derived:ebitda_approx). Bare _in_filings (just "is this digit
                    # string anywhere in the filing") is too loose to trust here — a wrong
                    # number can trivially be SOME real figure elsewhere in a 10-Q — so
                    # this uses the same proximity requirement _in_filings_near already
                    # applies everywhere else, fed by the source prose around the number,
                    # not just the short label (numwatch-009 follow-up, 2026-08-20).
                    hit = _in_filings_near(a, label + " " + context, filing_texts)
                    if hit:
                        return "in-filing", hit
                    return "mislabel", (f"labeled '{label}' but the value matches {src} — "
                                        "the LYFT-error shape (wrong concept under a familiar name)")
        # BEFORE crying unsourced: is the number simply IN THE FILING, under a line the
        # card does not carry as a concept? A property-level revenue, a segment figure, a
        # note disclosure — all real, sourced, quotable numbers that no consolidated card
        # concept will ever match. This path existed only for unknown labels, so any number
        # whose label happened to resemble a card concept was declared unsourced without
        # the filings ever being read. That was 162 of 289 open rows on 2026-08-18, 83 of
        # them on names we hold, and it trained the PM to skim a list built to be read.
        hit = _in_filings(a, filing_texts, literal) or _in_filings_near(a, label, filing_texts)
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
    hit = _in_filings(a, filing_texts, literal) or _in_filings_near(a, label, filing_texts)
    if hit:
        return "ok", hit
    return "unsourced", "unknown concept and no precise filing match"


def _find_amount(text, literal, a):
    """Locate a number's OWN occurrence in the source text, for context extraction
    (trace_number's qualifier-proximity check). A literal find(literal) often misses:
    extraction is told to copy the amount "EXACTLY as written" but still reformats it
    ("$758,685K" in the source became "$758.685M" in n["text"] — same value, different
    text, ARI numwatch-009 follow-up 2026-08-20). Try the literal first, then fall back
    to scale/decimal variants of the numeric value itself, the same style of form
    generation _in_filings already uses to match a value against filing text."""
    i = text.find(literal)
    if i >= 0:
        return i
    for scale in (1e6, 1e3, 1):
        v = a / scale
        if not (0.01 <= v < 1e6):
            continue
        for dec in (0, 1, 2, 3):
            for s in (f"{v:,.{dec}f}", f"{v:.{dec}f}"):
                s = s.rstrip("0").rstrip(".") if dec else s
                if len(re.sub(r"[^0-9]", "", s)) >= 3:
                    i = text.find(s)
                    if i >= 0:
                        return i
    return -1


def sweep_prose(tk, texts):
    """texts: {label: prose}. Returns unsourced-number findings for one name."""
    card = _j(NAMES / tk / "fincard.json", {})
    fdir = NAMES / tk / "filings"
    filing_texts = {p.name: p.read_text(errors="replace") for p in fdir.glob("*.txt")} if fdir.exists() else {}
    finds, seen_in_filing = [], []
    for label, text in texts.items():
        for n in extract_prose_numbers(text, label):
            i = _find_amount(text, n["text"], n["abs"])
            context = text[max(0, i - 80): i + 80] if i >= 0 else ""
            status, detail = trace_number(n["abs"], n["label"], card, filing_texts, context,
                                           literal=n["text"])
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
