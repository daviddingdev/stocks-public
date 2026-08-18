#!/usr/bin/env python3
"""
Fincard query — the PM computes by writing CODE over sourced figures,
never by doing arithmetic in its head (David, 2026-08-12).

Evaluates a Python expression where every fincard figure and derived value is
a variable (plus quarterly/annual series as lists, and math functions). Prints
each input used WITH its provenance, then the result — so the output pastes
into a memo as a self-documenting calculation.

  query.py TRIP "net_cash + 700e6*0.79"                    # post-TheFork net cash est.
  query.py LYFT "(cfo - 0.1e9) / market_cap * 100"         # FCF yield at assumed capex
  query.py ARI  "cash / shares_out"                        # cash per share
  query.py TLS  "sum(q['value'] for q in series_revenue_quarters[:4])"

Variables: every key in figures (value), derived (value), price, shares_out,
series_<name>_quarters / series_<name>_annual / series_<name>_points (lists of
dicts), plus min/max/sum/abs/round/len and math.*.

Card resolution: agent dossier (names/<TK>/fincard.json) first, then any research-book
evidence pack (*/research/_evidence/fincard.json with matching ticker), else
builds fresh via fincard.py. Works for both books.
"""
import json
import math
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
ROOT = ENGINE.parent


def find_card(tk):
    p = ENGINE / "agent" / "names" / tk / "fincard.json"
    if p.exists():
        return json.loads(p.read_text()), str(p)
    for ev in ROOT.glob(f"*-{tk}/research/_evidence/fincard.json"):
        return json.loads(ev.read_text()), str(ev)
    sys.path.insert(0, str(ENGINE / "valuation"))
    import fincard
    return fincard.build(tk), "(built fresh, not cached)"


def run(tk, expr):
    tk = tk.upper()
    card, src = find_card(tk)
    ns = {"min": min, "max": max, "sum": sum, "abs": abs, "round": round, "len": len,
          "math": math, "sorted": sorted}
    prov = {}
    for k, v in (card.get("figures") or {}).items():
        ns[k] = v.get("value")
        prov[k] = f"{v.get('value'):,} {v.get('unit', '')} · {v.get('period') or ('asof ' + str(v.get('asof')))} [{v.get('tag')}]"
    for k, v in (card.get("derived") or {}).items():
        ns[k] = v.get("value")
        prov[k] = f"{v.get('value'):,} = {v.get('formula', '')}"
    if card.get("price"):
        ns["price"] = card["price"]["value"]
        prov["price"] = f"{card['price']['value']} ({card['price'].get('source')}, {card['price'].get('asof')})"
    for name, s in (card.get("series") or {}).items():
        for part in ("quarters", "annual", "points"):
            if s.get(part):
                ns[f"series_{name}_{part}"] = s[part]
    try:
        result = eval(compile(expr, "<query>", "eval"), {"__builtins__": {}}, ns)
    except Exception as e:
        print(f"ERROR: {e}")
        avail = sorted(k for k in ns if not k.startswith("series_") and k not in
                       ("min", "max", "sum", "abs", "round", "len", "math", "sorted"))
        print("available variables:", ", ".join(avail))
        return 1
    used = sorted(k for k in prov if k in expr)
    print(f"# {tk} query — card {card.get('built')} ({src})")
    for k in used:
        print(f"#   {k} = {prov[k]}")
    for fl in card.get("flags", []):
        if any(k in fl for k in used) or "MISMATCH" in fl or "MIXED" in fl:
            print(f"#   FLAG: {fl}")
    print(f"{expr}\n= {result:,.4f}" if isinstance(result, float) else f"{expr}\n= {result}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: query.py TICKER "expression"')
    sys.exit(run(sys.argv[1], " ".join(sys.argv[2:])))
