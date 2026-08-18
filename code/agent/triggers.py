#!/usr/bin/env python3
"""
Trigger engine — coded, event-driven escalation instead of fixed clock times.

Cron runs this every 5 minutes during US market hours. Each run is cheap, pure
Python (Finnhub quotes + EDGAR + files already on disk). It does NOTHING unless
a rule trips; then it escalates in one of two ways:

  ALERT  -> append to data/alerts.json (shown on the dashboard /agent page) and
            push to David's phone via ntfy.sh (topic in config/triggers.json —
            install the ntfy app and subscribe to that topic).
  ACTION -> additionally launch a headless Claude decision session (loop.py
            trade) — ONLY for agent-book events, only while funded, and capped
            per day. This is how the agent book reacts to events in minutes
            instead of at the next cron slot; Claude still writes its memo
            before any order, per MANDATE.md.

Rules — HELD NAMES ONLY (David, 2026-07-30: no watchlist noise; every alert names the
book it hits and quantifies impact). Thresholds in config/triggers.json; dedupe in
data/trigger_state.json:
  1. Held name moves >= threshold (BROKERA: price_move_pct_holdings; agent: agent_action_move_pct,
     which also triggers an ACTION decision session) -> ALERT with book + $/% impact
  2. New 8-K/material filing TODAY by a held name -> ALERT (+ACTION if agent-held 8-K)
  3. New 13D on a held name / earnings today for a held name -> ALERT with position context
  4. Agent book drawdown >= agent_drawdown_alert_pct vs its funded baseline -> ALERT (David decides;
     no auto-liquidation — there are no kill conditions by design)

CLI: triggers.py run | triggers.py test-push
"""
import datetime as dt
import json
import pathlib
import os
import sys
from pathlib import Path

def _write_json(path, obj, indent=1):
    """Atomic write. feeds/triggers/mcp_sync all rewrite these files on cron while the
    PM session and the dashboard are reading them — feed.json in particular is written
    by the :00 feed refresh at the same minute the trade session starts. A plain
    write_text truncates first, so a reader can catch an empty or half-written file and
    conclude the world is empty. Same-dir temp + os.replace makes the swap atomic."""
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent))
    os.replace(tmp, path)


import requests

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
DATA = HERE / "data"
CONF = ENGINE / "config"
sys.path.insert(0, str(HERE))
import feeds  # noqa: E402  (fh_get, cik_map, UA, universe)
import loop   # noqa: E402

STATE_F = DATA / "trigger_state.json"
ALERTS_F = DATA / "alerts.json"
BASELINE = 10000.0


def _j(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def cfg():
    c = _j(CONF / "triggers.json", {})
    c.setdefault("price_move_pct_holdings", 5.0)
    c.setdefault("price_move_pct_watch", 8.0)
    c.setdefault("agent_action_move_pct", 7.0)
    c.setdefault("agent_drawdown_alert_pct", 10.0)
    c.setdefault("max_auto_trade_sessions_per_day", 3)
    c.setdefault("ntfy_topic", "")
    # thesis-vs-price contradiction watch (added 2026-08-12 — the missing LBRDP
    # tripwire: the error announced itself through STILLNESS, not a move)
    c.setdefault("thesis_gap_alert_pct", 12.0)      # market >=12% below memo's implied value
    c.setdefault("thesis_gap_persist_days", 10)     # ...for 10 consecutive market days
    # position drawdown vs COST -> forced document-first re-underwrite (pro practice:
    # a drawdown triggers a formal fresh-look review, not an auto-add or auto-sell)
    c.setdefault("reunderwrite_drawdown_pct", 15.0)
    # single industry/theme group >= this % of agent equity -> concentration alert
    c.setdefault("concentration_alert_pct", 30.0)
    return c


def held_symbols():
    brokera = [t for t, m in _j(CONF / "positions.json", {}).items() if (m.get("shares") or 0) > 0]
    agent = [p.get("symbol") for p in _j(DATA / "portfolio.json", {}).get("positions", []) if p.get("symbol")]
    return brokera, agent


def push(topic, title, msg):
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(),
                      headers={"Title": title, "Tags": "chart_with_upwards_trend"}, timeout=10)
    except Exception:
        pass


def alert(state, c, key, kind, symbol, msg, action=False, book=""):
    """Dedup by key (per day); record, push, and optionally launch a decision session.
    Held-names-only policy (David, 2026-07-30): every alert names the BOOK it hits
    (BROKERA / Agent / both) and quantifies portfolio impact in the message."""
    today = dt.date.today().isoformat()
    seen = state.setdefault("seen", {})
    if seen.get(key) == today:
        return False
    seen[key] = today
    alerts = _j(ALERTS_F, [])
    alerts.append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "kind": kind, "symbol": symbol, "msg": msg, "action": bool(action), "book": book})
    _write_json(ALERTS_F, alerts[-200:])
    push(c["ntfy_topic"], f"{book or 'Stocks'} · {kind}" + (f" · {symbol}" if symbol and symbol != "AGENT" else ""), msg)
    if action:
        n = state.setdefault("trade_sessions", {})
        if n.get(today, 0) < c["max_auto_trade_sessions_per_day"]:
            pf = _j(DATA / "portfolio.json", {})
            if (pf.get("cash") or 0) > 0 or (pf.get("total_value") or 0) > 0:
                r = loop.launch("trade")
                if r.get("ok"):
                    n[today] = n.get(today, 0) + 1
                    push(c["ntfy_topic"], "Stocks · agent", f"Decision session launched: {msg}")
    return True


def run():
    c = cfg()
    state = _j(STATE_F, {})

    # --- position context: who holds what, in which book, at what weight ---
    jpm_pos = {t: m for t, m in _j(CONF / "positions.json", {}).items() if (m.get("shares") or 0) > 0}
    jpm_total = _j(CONF / "account.json", {}).get("total_value") or 0
    ag_pf = _j(DATA / "portfolio.json", {})
    ag_pos = {p["symbol"]: p for p in ag_pf.get("positions", []) if p.get("symbol")}
    ag_total = ag_pf.get("total_value") or 0
    held = list(dict.fromkeys(list(jpm_pos) + list(ag_pos)))
    fired = 0

    def impact(tk, price, prev):
        """One line, both books: '<book> <value> (<share of total>), day <change>' each."""
        parts, books = [], []
        if tk in jpm_pos:
            sh = jpm_pos[tk].get("shares") or 0
            v = sh * price
            day = sh * (price - prev)
            w = f" ({v / jpm_total * 100:.1f}% of book)" if jpm_total else ""
            parts.append(f"BROKERA ${v / 1000:,.1f}k{w}, day {'-' if day < 0 else '+'}${abs(day):,.0f}")
            books.append("BROKERA")
        if tk in ag_pos:
            qty = ag_pos[tk].get("qty") or 0
            v = qty * price
            day = qty * (price - prev)
            w = f" ({v / ag_total * 100:.1f}% of book)" if ag_total else ""
            parts.append(f"Agent ${v / 1000:,.1f}k{w}, day {'-' if day < 0 else '+'}${abs(day):,.0f}")
            books.append("Agent")
        return " · ".join(parts), "+".join(books)

    # --- 1: price moves on HELD names only (watchlist noise removed 2026-07-30) ---
    for tk in held:
        q = feeds.fh_get("quote", symbol=tk) or {}
        price, prev = q.get("c"), q.get("pc")
        if not price or not prev:
            continue
        pct = (price - prev) / prev * 100
        is_agent = tk in ag_pos
        thr = c["agent_action_move_pct"] if is_agent else c["price_move_pct_holdings"]
        if abs(pct) >= thr:
            ctx, book = impact(tk, price, prev)
            fired += alert(state, c, f"move:{tk}", "price move", tk,
                           f"{tk} {pct:+.1f}% (${price:,.2f}) — {ctx}",
                           action=is_agent, book=book)

    # --- 1b: named price levels (sell/entry plans) — config "price_levels" ---
    for tk, lv in (c.get("price_levels") or {}).items():
        q = feeds.fh_get("quote", symbol=tk) or {}
        price, prev = q.get("c"), q.get("pc")
        if not price:
            continue
        hit = None
        if lv.get("above") and price >= lv["above"]:
            hit = f"crossed ABOVE ${lv['above']:,.2f}"
        elif lv.get("below") and price <= lv["below"]:
            hit = f"crossed BELOW ${lv['below']:,.2f}"
        if hit:
            ctx, book = impact(tk, price, prev or price)
            note = f" — {lv['note']}" if lv.get("note") else ""
            fired += alert(state, c, f"level:{tk}:{hit.split()[1]}", "price level", tk,
                           f"{tk} {hit} (now ${price:,.2f}) — {ctx or 'not held'}{note}", book=book or "Level")

    # --- 2: fresh material filings by held names ---
    m = feeds.cik_map()
    today_s = dt.date.today().isoformat()
    for tk in held:
        cik = m.get(tk)
        if not cik:
            continue
        try:
            rec = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                               headers=feeds.UA, timeout=20).json()["filings"]["recent"]
        except Exception:
            continue
        for form, date, acc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"]):
            if date != today_s or form not in feeds.INTERESTING:
                continue
            q = feeds.fh_get("quote", symbol=tk) or {}
            ctx, book = impact(tk, q.get("c") or 0, q.get("pc") or q.get("c") or 0)
            fired += alert(state, c, f"filing:{acc}", "filing", tk,
                           f"{tk} filed {form} today — holding: {ctx}",
                           action=(tk in ag_pos and form == "8-K"), book=book)

    # --- 3: radar 13Ds + earnings, HELD names only ---
    feed = _j(DATA / "feed.json", {})
    for r in (feed.get("situations") or {}).get("sc13d", []):
        tk = r.get("ticker")
        if tk in set(held):
            q = feeds.fh_get("quote", symbol=tk) or {}
            ctx, book = impact(tk, q.get("c") or 0, q.get("pc") or q.get("c") or 0)
            fired += alert(state, c, f"13d:{r['url']}", "13D stake", tk,
                           f"New 13D on {tk} ({r.get('company', '')[:40]}) — holding: {ctx}", book=book)
    # feed.json is agent-universe only since 2026-08-12 (book independence) — BROKERA
    # held names get a direct calendar query so David's earnings alerts survive.
    earn_rows = list(feed.get("earnings", []))
    feed_syms = {e.get("symbol") for e in earn_rows}
    for tk in jpm_pos:
        if tk in feed_syms:
            continue
        resp = feeds.fh_get("calendar/earnings", symbol=tk,
                            **{"from": today_s, "to": today_s})
        earn_rows += (resp or {}).get("earningsCalendar", [])
    for e in earn_rows:
        tk = e.get("symbol")
        if e.get("date") == today_s and tk in set(held):
            q = feeds.fh_get("quote", symbol=tk) or {}
            ctx, book = impact(tk, q.get("c") or 0, q.get("pc") or q.get("c") or 0)
            fired += alert(state, c, f"earn:{tk}:{today_s}", "earnings", tk,
                           f"{tk} reports today ({e.get('hour') or 'time n/a'}) — holding: {ctx}", book=book)

    # --- 4: agent-book drawdown ---
    if ag_total > 0 and (BASELINE - ag_total) / BASELINE * 100 >= c["agent_drawdown_alert_pct"]:
        fired += alert(state, c, f"dd:{int((BASELINE - ag_total) / BASELINE * 20)}", "drawdown", "AGENT",
                       f"Agent book at ${ag_total:,.0f} "
                f"({(ag_total - BASELINE) / BASELINE * 100:+.1f}% vs ${BASELINE:,.0f} baseline)",
                       book="Agent")

    today_s = dt.date.today().isoformat()
    week_s = dt.date.today().strftime("%G-W%V")

    # --- 5: thesis-vs-price contradiction (added 2026-08-12) ---
    # Anchors come from data/thesis.json, maintained by the agent per position:
    #   {"SYM": {"implied_value": N, "deadline": "YYYY-MM-DD", "theme": "...", "note": "..."}}
    # If the market sits >=X% below the memo's own implied value for N consecutive
    # market days, that is the market persistently disagreeing with the thesis —
    # evidence, not noise (LBRDP sat 13% below a believed 11-month par for 2 weeks
    # and nothing fired). Alert says: verify the premise in the DOCUMENTS.
    thesis = _j(DATA / "thesis.json", {})
    tstate = state.setdefault("thesis_gap", {})
    for tk, th in thesis.items():
        if tk not in ag_pos or not th.get("implied_value"):
            continue
        q = feeds.fh_get("quote", symbol=tk) or {}
        price = q.get("c")
        if not price:
            continue
        gap = (th["implied_value"] - price) / th["implied_value"] * 100
        rec = tstate.setdefault(tk, {"days": 0, "last": ""})
        if gap >= c["thesis_gap_alert_pct"]:
            if rec["last"] != today_s:      # count each market day once
                rec["days"] += 1
                rec["last"] = today_s
            n = c["thesis_gap_persist_days"]
            if rec["days"] >= n and (rec["days"] - n) % 5 == 0:
                dl = th.get("deadline", "no deadline set")
                fired += alert(state, c, f"thesisgap:{tk}:{rec['days']}", "thesis-vs-price", tk,
                               f"{tk} ${price:,.2f} has sat >={c['thesis_gap_alert_pct']:.0f}% below your "
                               f"implied ${th['implied_value']:,.2f} for {rec['days']} market days "
                               f"(deadline {dl}). The market is persistently disagreeing with your thesis — "
                               f"that is evidence, not noise. Re-verify the premise in the PRIMARY DOCUMENTS "
                               f"(dossier terms.json + filings), not in your own memos.", book="Agent")
        else:
            tstate[tk] = {"days": 0, "last": ""}

    # --- 6: position drawdown vs cost -> forced re-underwrite (added 2026-08-12) ---
    # Professional practice: a position down >=15% from cost gets a formal fresh-look
    # review. Fires an ACTION session at most once per ISO week per name; the session
    # must re-derive the thesis from documents FIRST, then read its own memos.
    for tk, p in ag_pos.items():
        pnl_pct = p.get("pnl_pct")
        if pnl_pct is None or pnl_pct > -c["reunderwrite_drawdown_pct"]:
            continue
        fired += alert(state, c, f"reunder:{tk}:{week_s}", "re-underwrite", tk,
                       f"{tk} {pnl_pct:+.1f}% vs cost ${p.get('avg_cost', 0):,.2f} — forced fresh-look: "
                       f"re-derive the thesis from the dossier/filings FIRST (do not start from your own "
                       f"memos), then decide add/hold/exit against your pre-committed triggers.",
                       action=True, book="Agent")

    # --- 7: book concentration by theme/industry (added 2026-08-12) ---
    # Correlated names are a book-level risk no per-position rule sees (LYFT+TRIP =
    # travel ~20% discovered only when oil hit both). Group = thesis.json "theme"
    # if set, else Finnhub industry (cached 30d).
    if ag_total > 0:
        icache = _j(DATA / "industry_cache.json", {})
        groups = {}
        for tk, p in ag_pos.items():
            g = (thesis.get(tk) or {}).get("theme")
            if not g:
                if tk not in icache:
                    prof = feeds.fh_get("stock/profile2", symbol=tk) or {}
                    icache[tk] = prof.get("finnhubIndustry") or "unknown"
                g = icache[tk]
            groups.setdefault(g, []).append((tk, p.get("value") or 0))
        (DATA / "industry_cache.json").write_text(json.dumps(icache, indent=1))
        for g, members in groups.items():
            if g == "unknown" or len(members) < 2:
                continue
            w = sum(v for _, v in members) / ag_total * 100
            if w >= c["concentration_alert_pct"]:
                fired += alert(state, c, f"conc:{g}:{week_s}", "concentration", "AGENT",
                               f"Agent book: {', '.join(t for t, _ in members)} are all '{g}' — "
                               f"{w:.0f}% of book moves together. One macro headline hits all of them.",
                               book="Agent")

    _write_json(STATE_F, state)
    print(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} triggers: {fired} fired "
          f"({len(jpm_pos)} brokera, {len(ag_pos)} agent holdings watched)")


if __name__ == "__main__":
    if sys.argv[1:2] == ["test-push"]:
        push(cfg()["ntfy_topic"], "Stocks · test", "Trigger engine connected — this is what alerts will look like.")
        print("test push sent to topic:", cfg()["ntfy_topic"])
    elif sys.argv[1:2] == ["run"]:
        run()
    else:
        sys.exit("usage: triggers.py run | triggers.py test-push")
