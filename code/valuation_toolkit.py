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
