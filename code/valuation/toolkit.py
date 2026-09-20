#!/usr/bin/env python3
"""
Valuation toolkit — the fit-for-purpose methods every company gets.

Teaching-grade: each function documents the concept so it doubles as a learning
reference. Pick the method by company type (see research/RUNBOOK.md):
  - profitable / cash-generative  -> dcf() + reverse_dcf()
  - pre-profit / pipeline / option -> rnpv_sotp() + reverse_target()
  - always, as a cross-check       -> comps_ev()

All money in the same unit (use $M consistently). No external deps.
"""
from __future__ import annotations


def dcf(fcfs, discount_rate, terminal_growth, net_cash=0.0, shares=None):
    """Discounted Cash Flow — intrinsic value from projected free cash flows.

    Idea: a business is worth the cash it will produce, discounted back to today
    (a dollar next year is worth less than a dollar now). We sum the present
    value of each projected FCF, add a 'terminal value' for everything after the
    explicit forecast (Gordon growth), then adjust for net cash/debt.

    fcfs            list of projected free cash flows, year 1..N ($M)
    discount_rate   e.g. 0.10 = 10% (WACC / required return)
    terminal_growth long-run growth after the forecast, e.g. 0.025 (< discount_rate!)
    net_cash        cash minus debt ($M); added to equity value
    shares          shares outstanding (M) -> returns per-share value
    """
    assert terminal_growth < discount_rate, "terminal growth must be < discount rate"
    pv = sum(f / (1 + discount_rate) ** (i + 1) for i, f in enumerate(fcfs))
    tv = fcfs[-1] * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_tv = tv / (1 + discount_rate) ** len(fcfs)
    ev = pv + pv_tv
    equity = ev + net_cash
    out = {"pv_explicit": pv, "pv_terminal": pv_tv, "enterprise_value": ev,
           "equity_value": equity, "terminal_pct": pv_tv / ev if ev else None}
    if shares:
        out["per_share"] = equity / shares
    return out


def reverse_dcf(price, shares, base_fcf, discount_rate, terminal_growth, years, net_cash=0.0):
    """Reverse-DCF — what FCF growth is the CURRENT PRICE implying?

    Instead of guessing growth to get a value, we invert: solve for the constant
    growth rate g that makes the DCF equal today's price. Then ask: 'is that
    growth realistic?' If the market implies 25%/yr forever and the company grows
    8%, it's expensive; if it implies -2% and the company grows 10%, it's cheap.
    Great for cutting through optimistic models — it shows what's already priced in.
    """
    target_equity = price * shares
    lo, hi = -0.50, 1.00
    for _ in range(80):
        g = (lo + hi) / 2
        fcfs = [base_fcf * (1 + g) ** y for y in range(1, years + 1)]
        eq = dcf(fcfs, discount_rate, terminal_growth, net_cash)["equity_value"]
        if eq > target_equity:
            hi = g
        else:
            lo = g
    return {"implied_growth": (lo + hi) / 2}


def rnpv_sotp(programs, net_cash=0.0, shares=1.0):
    """risk-adjusted NPV / sum-of-the-parts — for optionality names (biotech, early tech).

    A DCF is garbage-in when there are no positive cash flows yet. Instead we
    value each 'shot on goal' separately and add them up. Each program:
      value = peak_opportunity * economics * prob_of_success * npv_multiple
    where economics = the fraction WE capture (royalty %, or margin), pos = odds
    it reaches market, and npv_multiple compresses a ramping, time-discounted,
    finite-life royalty stream into a single number (~4-8x peak is typical).

    programs: list of dicts {name, peak, economics, pos, mult}
    Returns total equity value + per share + a per-program breakdown.
    """
    rows = []
    for p in programs:
        peak_econ = p["peak"] * p["economics"]           # peak $ that reaches us
        val = peak_econ * p["pos"] * p["mult"]            # risk-adjusted NPV
        rows.append({**p, "peak_econ": peak_econ, "rnpv": val})
    total = sum(r["rnpv"] for r in rows) + net_cash
    return {"programs": rows, "sum_rnpv": total - net_cash, "net_cash": net_cash,
            "equity_value": total, "per_share": total / shares if shares else None}


def reverse_target(target_price, shares, net_cash, fixed_rnpv, driver):
    """Reverse-valuation — what does the KEY DRIVER have to do to reach a target price?

    (The 'what has to be true' pattern.) Hold everything else fixed and solve for what the
    swing asset must be worth. driver = {economics, pos, mult} for the swing program;
    returns the peak opportunity it must reach.
    """
    need_equity = target_price * shares
    need_from_driver = need_equity - net_cash - fixed_rnpv
    per_peak = driver["economics"] * driver["pos"] * driver["mult"]
    return {"driver_rnpv_needed": need_from_driver,
            "driver_peak_needed": need_from_driver / per_peak if per_peak else None}


def comps_ev(metric_value, multiple, net_cash=0.0, shares=None):
    """Relative valuation — value off what peers trade at.

    EV = metric (e.g. next-yr sales or EBITDA) * a peer multiple (EV/Sales, EV/EBITDA).
    Always run this as a sanity check on an intrinsic (DCF/rNPV) number.
    """
    ev = metric_value * multiple
    equity = ev + net_cash
    out = {"enterprise_value": ev, "equity_value": equity}
    if shares:
        out["per_share"] = equity / shares
    return out


def scenarios(fn, cases):
    """Run a valuation fn across named cases (bear/base/bull). cases = {name: kwargs}."""
    return {name: fn(**kw) for name, kw in cases.items()}


# ---------------------------------------------------------------- LBO (2026-09-20, David: "add the LBO modelling capability")
def irr(cashflows, lo=-0.99, hi=10.0, tol=1e-7):
    """Internal rate of return by bisection: the rate at which the NPV of `cashflows`
    (year 0 first, negative = money out) is zero. Returns None when no sign change."""
    def npv(r):
        return sum(cf / (1 + r) ** i for i, cf in enumerate(cashflows))
    if not cashflows or not (min(cashflows) < 0 < max(cashflows)):
        return None
    a, b = lo, hi
    fa, fb = npv(a), npv(b)
    if fa * fb > 0:
        return None
    for _ in range(200):
        m = (a + b) / 2
        fm = npv(m)
        if abs(fm) < tol:
            return m
        if fa * fm < 0:
            b, fb = m, fm
        else:
            a, fa = m, fm
    return (a + b) / 2


def lbo(ebitda, entry_multiple, leverage_turns=4.5, rate=0.09, years=5, ebitda_growth=0.06,
        exit_multiple=None, capex_pct=0.03, nwc_pct=0.01, tax_rate=0.25, fees_pct=0.02,
        revenue=None, da_pct_ebitda=0.25, cash_sweep=1.0, min_cash=0.0):
    """Leveraged buyout — the interview-paper LBO, in code, with every line shown.

    Idea: buy the business with mostly borrowed money, use its cash flow to pay the debt
    down, sell it later. Equity return = (exit equity / entry equity) — driven by three
    things and only three: EBITDA growth, multiple expansion (exit vs entry), and
    deleveraging (debt paid down with the company's own cash). The `bridge` below splits
    the equity gain into exactly those three so the story is visible, not a black box.

    ebitda          entry-year EBITDA ($M, TTM)
    entry_multiple  EV / EBITDA paid
    leverage_turns  debt raised as a multiple of EBITDA (LMM 2026: 3–4.5x; large cap 5–6x)
    rate            blended cash interest on the debt (0.09 = 9%)
    years           hold period
    ebitda_growth   annual EBITDA growth through the hold
    exit_multiple   EV / EBITDA at exit (default = entry: no multiple expansion assumed)
    capex_pct       capex as % of REVENUE if `revenue` is given, else % of EBITDA
    nwc_pct         working-capital investment as % of the revenue (or EBITDA) INCREASE
    tax_rate        cash tax on EBIT (EBITDA - D&A) less interest
    fees_pct        transaction fees as % of EV (a use of funds, paid by equity)
    da_pct_ebitda   D&A as % of EBITDA (tax shield proxy when no revenue given)
    cash_sweep      share of free cash flow applied to debt (1.0 = all of it)

    Returns a dict: sources_uses, schedule (per year), exit, returns (moic, irr) and bridge.
    All money in the unit `ebitda` was given in ($M recommended).
    """
    exit_multiple = entry_multiple if exit_multiple is None else exit_multiple
    ev = ebitda * entry_multiple
    debt0 = ebitda * leverage_turns
    fees = ev * fees_pct
    equity0 = ev + fees - debt0
    if equity0 <= 0:
        raise ValueError("leverage exceeds the purchase price — equity check would be negative")
    base = revenue if revenue else ebitda
    sched, debt, cash, e = [], debt0, 0.0, ebitda
    prev_base = base
    for y in range(1, years + 1):
        e = e * (1 + ebitda_growth)
        cur_base = prev_base * (1 + ebitda_growth)
        da = e * da_pct_ebitda
        interest = debt * rate
        ebit = e - da
        taxes = max(0.0, (ebit - interest)) * tax_rate
        capex = cur_base * capex_pct
        nwc = max(0.0, cur_base - prev_base) * nwc_pct
        fcf = e - interest - taxes - capex - nwc
        paydown = min(debt, max(0.0, fcf) * cash_sweep) if fcf > 0 else 0.0
        debt -= paydown
        cash += fcf - paydown
        sched.append({"year": y, "ebitda": round(e, 2), "interest": round(interest, 2), "taxes": round(taxes, 2),
                      "capex": round(capex, 2), "nwc": round(nwc, 2), "fcf": round(fcf, 2), "paydown": round(paydown, 2),
                      "debt_end": round(debt, 2), "cash_end": round(cash, 2), "leverage": round(debt / e, 2) if e else None})
        prev_base = cur_base
    exit_ev = e * exit_multiple
    exit_equity = exit_ev - debt + cash
    moic = exit_equity / equity0
    r = irr([-equity0] + [0.0] * (years - 1) + [exit_equity])
    # the bridge: three drivers, additive to (exit_equity - equity0) up to rounding
    g_ebitda = (e - ebitda) * entry_multiple                  # more EBITDA at the entry multiple
    g_multiple = (exit_multiple - entry_multiple) * e         # every exit dollar of EBITDA re-rated
    g_delever = (debt0 - debt) + cash                         # debt gone + cash built
    g_fees = -fees
    return {
        "inputs": {"ebitda": ebitda, "entry_multiple": entry_multiple, "leverage_turns": leverage_turns, "rate": rate,
                   "years": years, "ebitda_growth": ebitda_growth, "exit_multiple": exit_multiple, "capex_pct": capex_pct,
                   "nwc_pct": nwc_pct, "tax_rate": tax_rate, "fees_pct": fees_pct, "revenue": revenue},
        "sources_uses": {"enterprise_value": round(ev, 2), "fees": round(fees, 2), "debt": round(debt0, 2),
                         "equity": round(equity0, 2), "equity_pct": round(equity0 / (ev + fees) * 100, 1)},
        "schedule": sched,
        "exit": {"ebitda": round(e, 2), "ev": round(exit_ev, 2), "debt": round(debt, 2), "cash": round(cash, 2),
                 "equity": round(exit_equity, 2), "leverage": round(debt / e, 2) if e else None},
        "returns": {"moic": round(moic, 2), "irr_pct": round(r * 100, 1) if r is not None else None,
                    "equity_gain": round(exit_equity - equity0, 2)},
        "bridge": {"ebitda_growth": round(g_ebitda, 2), "multiple_change": round(g_multiple, 2),
                   "deleveraging": round(g_delever, 2), "fees": round(g_fees, 2)},
    }


def lbo_grid(rows, cols, row_key, col_key, **kw):
    """Sensitivity: IRR% for every (row, col) pair of two inputs, e.g.
    lbo_grid([7,8,9], [7,8,9,10], "entry_multiple", "exit_multiple", ebitda=100).
    A cell is None where the deal cannot be financed (equity ≤ 0)."""
    out = []
    for rv in rows:
        line = []
        for cv in cols:
            a = dict(kw); a[row_key] = rv; a[col_key] = cv
            try:
                line.append(lbo(**a)["returns"]["irr_pct"])
            except ValueError:
                line.append(None)
        out.append(line)
    return {"rows": rows, "cols": cols, "row_key": row_key, "col_key": col_key, "irr_pct": out}


def lbo_max_entry(target_irr_pct, lo=2.0, hi=40.0, **kw):
    """The HIGHEST entry EV/EBITDA that still returns `target_irr_pct` (exit multiple follows
    entry unless `exit_multiple` is passed). The interview question in reverse: "what could you
    pay?" Bisection on lbo(); None when even `lo` misses the target."""
    def irr_at(m):
        a = dict(kw); a["entry_multiple"] = m
        if "exit_multiple" not in kw:
            a["exit_multiple"] = m
        try:
            r = lbo(**a)["returns"]["irr_pct"]
        except ValueError:
            return None
        return r
    lo = max(lo, kw.get("leverage_turns", 4.5) * (1 + kw.get("fees_pct", 0.02)) + 0.25)   # below this the equity check is negative
    if (irr_at(lo) or -1) < target_irr_pct:
        return None
    a, b = lo, hi
    for _ in range(60):
        m = (a + b) / 2
        r = irr_at(m)
        if r is not None and r >= target_irr_pct:
            a = m
        else:
            b = m
    return round(a, 2)
