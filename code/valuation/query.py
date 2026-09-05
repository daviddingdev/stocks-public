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
import re
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
    # IDENTIFIERS, not substrings (hunt 2026-09-05). `k in expr` made every variable whose
    # name is a substring of another one look like an input to a calculation that never
    # referenced it: `query.py TLS "net_cash / shares_out"` listed `cash = 50,647,000` with
    # full provenance among its inputs. A query printout is pasted into memos as the proof
    # of a number (MANDATE rail 7) — an input line that names a figure the expression never
    # read is a false provenance claim, in the one artifact whose whole job is provenance.
    _idents = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", expr))
    used = sorted(k for k in prov if k in _idents)
    print(f"# {tk} query — card {card.get('built')} ({src})")
    for k in used:
        print(f"#   {k} = {prov[k]}")
    # FLAG MATCHING IS CASE- AND PUNCTUATION-BLIND (hunt 2026-09-05). fincard writes its
    # flags as English prose in caps — "NET CASH UNRELIABLE: debt_lt tag STALE (last known
    # 11,500,000 at 2019-12-31) excluded" — while the variables are snake_case, so a plain
    # `"net_cash" in fl` matched NOTHING and the flag written to stop that exact number
    # being trusted was the one flag a `query.py COLL "net_cash"` did not print. COLL's card
    # carries six flags; the query showed three, and both suppressed ones were the debt
    # staleness that makes its 129,467,000 "net cash" wrong against ~1.09B of loans payable
    # the card can see but not resolve. Normalise both sides before matching, and always
    # print a flag that invalidates the card as a whole rather than one named figure.
    _ALWAYS = ("MISMATCH", "MIXED", "UNRELIABLE", "DOES NOT FOOT", "IDENTITY FAILS")
    def _norm(t):
        return re.sub(r"[^a-z0-9]+", "_", t.lower())
    for fl in card.get("flags", []):
        nfl = _norm(fl)
        if any(k in nfl for k in used) or any(a in fl for a in _ALWAYS):
            print(f"#   FLAG: {fl}")
    print(f"{expr}\n= {result:,.4f}" if isinstance(result, float) else f"{expr}\n= {result}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: query.py TICKER "expression"')
    sys.exit(run(sys.argv[1], " ".join(sys.argv[2:])))
