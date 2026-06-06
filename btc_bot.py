"""
btc_bot.py — Multi-crypto paper-trading bot.

One cycle:
  1. fetch a focused crypto universe from TradingView (prices + skill indicators)
  2. enforce pre-committed stop-loss / take-profit on open positions
  3. ask Claude for a set of orders (guided by philosophy.md + the skills)
  4. execute on the VIRTUAL multi-asset account (max 4 positions)
  5. log + optional Telegram

100% simulated money. Never sends real orders.

CLI:
    python btc_bot.py --once
    python btc_bot.py --loop 900
    python btc_bot.py --once --telegram
    python btc_bot.py --reset
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic

import btc_feed
import market_data
import paper_broker
import prompts
from telegram_alert import send_telegram

MODEL = "claude-sonnet-4-6"
MAX_POSITIONS = 3               # scan many, act on few — selective
MAX_POSITION_PCT = 65.0         # cap any single position at 65% of equity → Claude weights freely
FULLY_INVESTED = True           # ALWAYS deploy ~100% of cash — no idle liquidity
MIN_CASH_SWEEP = 5.0            # below this, leftover cash is left alone

_client: anthropic.Anthropic | None = None
_LABELS = [e["label"] for e in btc_feed.CRYPTO_UNIVERSE]
_LABEL_TO_TV = {e["label"]: e["tv"] for e in btc_feed.CRYPTO_UNIVERSE}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string", "description": "2-3 sentences: crypto regime read + book strategy."},
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "enum": _LABELS},
                    "action": {"type": "string", "enum": ["BUY", "SELL", "SHORT", "COVER", "HOLD"]},
                    "size_pct": {"type": "number", "description": "BUY/SHORT: % of free cash used as MARGIN. SELL/COVER: % of that position. 0 for HOLD."},
                    "leverage": {"type": "number", "description": "BUY/SHORT: leverage 1 to 5 (exposure = margin × leverage). Use 1 for SELL/COVER/HOLD."},
                    "confidence": {"type": "number", "description": "0 to 1."},
                    "thesis": {"type": "string", "description": "Max 2 sentences."},
                    "stop_loss": {"type": ["number", "null"]},
                    "take_profit": {"type": ["number", "null"]},
                },
                "required": ["symbol", "action", "size_pct", "leverage", "confidence", "thesis", "stop_loss", "take_profit"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rationale", "orders"],
    "additionalProperties": False,
}

DECISION_TASK = f"""\
You actively manage a VIRTUAL multi-crypto paper account (EUR), and you can go \
both LONG and SHORT. Build a FOCUSED book and aim to profit in BOTH directions — \
make money when the market rises (long) AND when it falls (short).

Universe: {", ".join(_LABELS)}.

Actions per symbol:
- BUY   = open/add a LONG (profit if price rises). size_pct = % of free cash used as MARGIN; leverage = 1–5x.
- SELL  = reduce/close a LONG. size_pct = % of that long. leverage = 1.
- SHORT = open/add a SHORT (profit if price FALLS). size_pct = % of free cash used as MARGIN; leverage = 1–5x.
- COVER = reduce/close a SHORT. size_pct = % of that short. leverage = 1.
- HOLD  = do nothing on that name. leverage = 1.

Rules (philosophy + skills):
- LEVERAGE 1x–5x, chosen PER TRADE via the `leverage` field (both longs and \
shorts). size_pct sets the MARGIN (% of free cash committed); EXPOSURE = margin × \
leverage. Margin is locked from cash, so total margin across positions never \
exceeds your capital — but leverage scales BOTH gains and losses on that margin. \
Choose leverage from CONVICTION + VOLATILITY: default 1x–2x; use 3x only on clean, \
high-probability setups; reserve 4x–5x for the very best, low-noise structures. A \
symbol is either long OR short — close it to flip.
- LIQUIDATION RISK: a position is force-closed and its MARGIN IS LOST if price \
moves ~1/leverage against your entry (≈50% at 2x, ≈33% at 3x, ≈25% at 4x, ≈20% at \
5x). Your pre-committed stop_loss MUST sit INSIDE that liquidation distance so the \
stop fills first — never place a stop wider than the liquidation move, and if a \
sound technical stop would fall beyond it, LOWER the leverage until the stop fits. \
Higher leverage therefore demands a tighter, well-defined technical stop. \
SAFETY NET: the system auto-reduces your requested leverage if it would put \
liquidation within ~1.5× the stop distance, so the stop always fills before \
liquidation — but still size leverage to your stop yourself; do not rely on the \
cap, as it only lowers (never raises) and a clipped leverage means smaller size.
- HORIZON: trades are SHORT — minutes to a few hours. Open with that outlook and \
size stops/targets to that breath (read them from the short-timeframe structure: \
recent minute/hourly highs-lows and Bollinger bands), NOT wide multi-day swing stops.
- DIRECTION vs TIMING (key rule): the DAILY window (with WEEKLY/MONTHLY as the \
background tide) sets the ALLOWED DIRECTION; HOURLY confirms it. MINUTE and HOURLY \
are used to TIME the entry and read momentum WITHIN that direction — NEVER to flip \
you counter-trend. A minute MACD turning positive inside a DAILY DOWNTREND is a \
chance to SHORT into strength, NOT a reason to go long. So: daily downtrend → only \
SHORT or cash (time the short on a minute up-tick toward resistance); daily uptrend \
→ only LONG (time on a minute dip). The hold horizon stays minutes/hours. If the \
daily is flat/choppy, take only a SMALL, LOW-leverage position in the marginally \
favoured direction (or give that slot to a clearer name) — the book still stays \
fully deployed.
- LOCAL HIGHS/LOWS: each asset also lists recent local highs/lows per timeframe \
(support below the price / resistance above). Use them to ANTICIPATE BOUNCES — \
near strong multi-timeframe SUPPORT a rebound is more likely (favour long / cover \
shorts / take profit on shorts); near strong RESISTANCE a rejection is more likely \
(favour short / take profit on longs). Levels confirmed across timeframes matter \
more. Put stops just beyond these levels and targets at the next opposite level. \
Act on confirmation (price holds the level + momentum turns), not blindly.
- TRADE WITH THE DOMINANT TREND, never against it. In a confirmed DOWNTREND the \
bias is SHORT or cash — do NOT buy oversold dips (counter-trend longs keep getting \
stopped out). SHORT the weakest names EVEN WHEN RSI IS LOW, as long as momentum \
stays negative (a low RSI alone is NOT a reason to skip a short in a falling \
market). Only skip a short at CONFIRMED EXHAUSTION: extreme RSI (≲10) AND a \
reversal actually underway (minute MACD turning positive / bullish divergence). \
In a confirmed UPTREND prefer LONGS on the strongest; in chop, stay light \
(small size, low leverage) but still deployed.
- STAY FULLY INVESTED: deploy ~100% of free cash EVERY cycle across \
{MAX_POSITIONS} positions — holding idle cash or staying flat is NOT allowed. \
Fill all {MAX_POSITIONS} slots with the best opportunities available right now: \
LONG the strongest names in an uptrend, SHORT the weakest in a downtrend. \
Conviction still decides the DIRECTION, the size and the leverage of each name \
(weakest setups → smaller size + lower leverage), but it never justifies sitting \
in cash. Set size_pct so the orders together commit essentially all free cash \
(any residual is auto-swept into the book). Spread across {MAX_POSITIONS} names \
rather than piling everything into one.
- EVERY new position needs a ≤2-sentence thesis AND BOTH a pre-committed
  stop_loss AND take_profit (NEVER null). Place them at TECHNICAL LEVELS read
  from the data — the recent high/low of the window, the Bollinger bands, nearby
  support/resistance — NOT at a fixed percentage. The distance follows the chart
  structure/volatility, not a preset risk number:
  · LONG  → stop_loss just below the recent low / lower Bollinger band;
    take_profit at the recent high / upper band / next resistance.
  · SHORT → stop_loss just above the recent high / upper band;
    take_profit at the recent low / lower band / next support.
  Prefer setups where these chart levels give a favourable reward:risk (target
  roughly ≥ 2× the stop distance); if a name offers poor R:R, give that slot to a \
  better name rather than holding cash.
- When a stop OR a target is hit, the position auto-closes; the next cycle
  re-decides from scratch (re-enter same direction, flip, or stay out). Take the
  profit at the target — do NOT "let it run".
- Exit early on thesis/trend break (MACD cross or EMA50 reclaim against you),
  even before the target. Never widen a stop to give a loser "another chance".

Return a set of orders. Respond strictly in the required JSON schema."""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def decide(snaps: dict, state: dict) -> dict[str, Any]:
    market_md = btc_feed.format_universe(snaps)
    account_md = paper_broker.format_state(state)
    recent_md = paper_broker.format_recent_trades(6)
    context = (
        "=== CRYPTO PAPER-TRADING BOT — LIVE CONTEXT ===\n"
        f"{market_md}\n"
        "Your virtual account:\n"
        f"{account_md}\n\n"
        f"Constraints: max {MAX_POSITIONS} open positions; any single position's "
        f"MARGIN capped at {MAX_POSITION_PCT:.0f}% of account value (leverage 1–5x "
        f"multiplies exposure on top of that margin).\n\n"
        "Recent trades:\n"
        f"{recent_md}\n"
        "=== END CONTEXT ==="
    )
    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=16000,   # high ceiling so the decision JSON is never truncated
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        system=[
            {"type": "text", "text": prompts.stable_system_block(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context},
        ],
        messages=[{"role": "user", "content": DECISION_TASK}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Truncated / malformed output → do nothing this cycle instead of crashing.
        return {"rationale": "Output del modello non valido o troncato — nessuna azione questo ciclo.",
                "orders": []}


def _execute_orders(decision: dict, prices: dict, snaps: dict) -> list[dict[str, Any]]:
    """Apply orders: closes/reductions (SELL, COVER) first to free cash, then opens (BUY, SHORT) with caps."""
    orders = decision.get("orders", [])
    closing = [o for o in orders if o["action"] in ("SELL", "COVER")]
    opening = [o for o in orders if o["action"] in ("BUY", "SHORT")]
    results: list[dict[str, Any]] = []

    for o in closing:
        tv = _LABEL_TO_TV.get(o["symbol"])
        if not tv or tv not in prices:
            continue
        results.append(paper_broker.execute_order(
            symbol=tv, label=o["symbol"], action=o["action"], size_pct=o["size_pct"],
            price=prices[tv], confidence=o.get("confidence"), thesis=o.get("thesis"),
            source=snaps.get(tv, {}).get("source")))

    # Recompute after closes free up cash / slots.
    state = paper_broker.get_state(prices)
    equity = state["value"]
    held = {p["symbol"] for p in state["positions"]}
    # Cap is on MARGIN committed per position (leverage multiplies exposure on top).
    used = {p["symbol"]: (p.get("collateral") or 0.0) for p in state["positions"]}
    cap_value = equity * MAX_POSITION_PCT / 100.0

    for o in opening:
        tv = _LABEL_TO_TV.get(o["symbol"])
        if not tv or tv not in prices:
            continue
        if tv not in held and len(held) >= MAX_POSITIONS:
            results.append({"executed": False, "label": o["symbol"], "action": o["action"],
                            "detail": f"{o['action']} {o['symbol']} skipped — max {MAX_POSITIONS} posizioni."})
            continue
        eur_cap = max(0.0, cap_value - used.get(tv, 0.0))
        res = paper_broker.execute_order(
            symbol=tv, label=o["symbol"], action=o["action"], size_pct=o["size_pct"],
            price=prices[tv], eur_cap=eur_cap, confidence=o.get("confidence"),
            thesis=o.get("thesis"), stop_loss=o.get("stop_loss"),
            take_profit=o.get("take_profit"), source=snaps.get(tv, {}).get("source"),
            leverage=o.get("leverage"))
        if res["executed"]:
            held.add(tv)
            used[tv] = used.get(tv, 0.0) + res["eur_amount"]
        results.append(res)

    return results


def _sweep_cash(prices: dict, snaps: dict) -> list[dict[str, Any]]:
    """
    Full-deployment policy: distribute ALL leftover cash across the currently held
    positions — BOTH longs (add via BUY) and shorts (add via SHORT) — proportional
    to remaining MARGIN room under the per-position cap, so the book stays ~fully
    invested. Each top-up keeps the position's existing leverage, stop and target.
    """
    state = paper_broker.get_state(prices)
    cash, equity = state["cash_eur"], state["value"]
    positions = state["positions"]
    if cash < MIN_CASH_SWEEP or not positions:
        return []

    cap_value = equity * MAX_POSITION_PCT / 100.0  # cap is on committed MARGIN
    rooms = {p["symbol"]: (p, max(0.0, cap_value - (p.get("collateral") or 0.0))) for p in positions}
    total_room = sum(r for _, r in rooms.values())
    if total_room <= 0:
        return []

    results = []
    for sym, (p, room) in rooms.items():
        alloc = min(cash * room / total_room, room)
        if alloc < MIN_CASH_SWEEP:
            continue
        action = "BUY" if p["side"] == "long" else "SHORT"
        res = paper_broker.execute_order(
            symbol=sym, label=p["label"], action=action, size_pct=100.0,
            price=prices.get(sym, p["price"]), eur_cap=alloc, confidence=None,
            thesis="Full-deployment sweep — no idle cash.",
            leverage=p.get("leverage") or 1.0,
            source=snaps.get(sym, {}).get("source"),
        )
        if res["executed"]:
            results.append(res)
    return results


FLAT_ANALYSIS_MIN_MINUTES = 60  # min minutes between non-exit (flat / free-capacity) analyses
FREE_CAPACITY_MIN_CASH = 50.0   # only re-analyze idle cash if at least this much is free


def _exit_checks() -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Enforce stops/targets — intrabar (level fill) + current price. NO Claude call."""
    prices = btc_feed.live_prices()
    exits: list[dict[str, Any]] = []
    tv_to_cg = {e["tv"]: e["cg"] for e in btc_feed.CRYPTO_UNIVERSE}
    for p in paper_broker.get_positions():
        cg = tv_to_cg.get(p["symbol"])
        if not cg:
            continue
        dec = paper_broker.intrabar_exit_decision(p, market_data.recent_hilo(cg))
        if dec:
            action = "SELL" if p["side"] == "long" else "COVER"
            res = paper_broker.execute_order(
                symbol=p["symbol"], label=p["label"], action=action, size_pct=100.0,
                price=dec["level"], confidence=1.0, thesis=dec["reason"], source="auto-exit")
            res["exit_reason"] = dec["reason"]
            exits.append(res)
    exits += paper_broker.check_exits(prices)
    return exits, prices


def analyze_and_trade(auto_exits: list | None = None) -> dict[str, Any]:
    """Full Claude analysis + order execution. Records a snapshot + last_analysis_ts."""
    snaps = btc_feed.get_universe_snapshot()
    prices = btc_feed.prices_from_snapshots(snaps)
    if not prices:
        return {"ok": False, "detail": "No market data available — skipped (no trade)."}

    state_before = paper_broker.get_state(prices)
    decision = decide(snaps, state_before)
    results = _execute_orders(decision, prices, snaps)
    if FULLY_INVESTED:
        results += _sweep_cash(prices, snaps)
    state = paper_broker.get_state(prices)

    paper_broker.record_snapshot(state["value"], prices.get("BINANCE:BTCEUR"))
    paper_broker.set_meta("last_analysis_ts", datetime.now(timezone.utc).isoformat())

    return {"ok": True, "snaps": snaps, "prices": prices, "auto_exits": auto_exits or [],
            "decision": decision, "results": results, "state": state}


def run_cycle(*, notify_telegram: bool = False) -> dict[str, Any]:
    """One full cycle: enforce exits, then a complete Claude analysis (used by --once)."""
    paper_broker.init_paper()
    auto_exits, _ = _exit_checks()
    out = analyze_and_trade(auto_exits)
    if notify_telegram and out.get("ok"):
        send_telegram(_telegram_text(out))
    return out


def watch_cycle() -> dict[str, Any]:
    """
    Lightweight watch (every ~10 min). Enforces stops/targets WITHOUT Claude.
    Triggers a full Claude analysis when:
      (1) a position exits → immediately, or
      (2) flat, or (3) there's free capacity (idle cash + a free slot) →
          throttled to at most every FLAT_ANALYSIS_MIN_MINUTES.
    """
    paper_broker.init_paper()
    auto_exits, prices = _exit_checks()
    state = paper_broker.get_state(prices)

    trigger = None
    if auto_exits:
        trigger = "exit"  # always immediate
    else:
        last = paper_broker.get_meta("last_analysis_ts")
        due = True
        if last:
            try:
                due = (datetime.now(timezone.utc) - datetime.fromisoformat(last)) \
                      >= timedelta(minutes=FLAT_ANALYSIS_MIN_MINUTES)
            except Exception:
                due = True
        if due:
            if state["n_positions"] == 0:
                trigger = "flat"
            elif state["cash_eur"] >= FREE_CAPACITY_MIN_CASH and state["n_positions"] < MAX_POSITIONS:
                trigger = "free-capacity"

    out = {"ok": True, "watch": True, "auto_exits": auto_exits,
           "trigger": trigger, "analysis": None, "state": state}
    if trigger:
        out["analysis"] = analyze_and_trade(auto_exits)
        if out["analysis"].get("ok"):
            out["state"] = out["analysis"]["state"]
    return out


def _telegram_text(out: dict[str, Any]) -> str:
    s = out["state"]
    lines = ["🤖 Crypto paper bot"]
    for ex in out.get("auto_exits", []):
        lines.append(f"⚠️ {ex['exit_reason']}")
    executed = [r for r in out["results"] if r.get("executed")]
    if executed:
        for r in executed:
            lines.append(f"• {r['detail']}")
    else:
        lines.append("• No new trades this cycle.")
    pos = ", ".join(f"{p['label']} {p['value']:,.0f}€" for p in s["positions"]) or "all cash"
    lines.append(f"Book: {pos}")
    lines.append(f"Value €{s['value']:,.2f} | P&L {s['pnl_pct']:+.2f}% | cash €{s['cash_eur']:,.0f}")
    return "\n".join(lines)


def _print_cycle(out: dict[str, Any]) -> None:
    if not out["ok"]:
        print(out["detail"])
        return
    s = out["state"]
    print(f"\n=== Crypto paper bot ({next(iter(out['snaps'].values()))['source']}) ===")
    print(f"Rationale: {out['decision'].get('rationale', '')}")
    for ex in out.get("auto_exits", []):
        print(f"AUTO-EXIT: {ex['detail']}")
    for r in out["results"]:
        print(("  [x] " if r.get("executed") else "  [ ] ") + r["detail"])
    print(f"Account: €{s['value']:,.2f}  P&L {s['pnl_pct']:+.2f}%  "
          f"(cash €{s['cash_eur']:,.2f}, {s['n_positions']} positions)")
    for p in s["positions"]:
        pnl = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "—"
        print(f"    {p['label']}: €{p['value']:,.2f} ({pnl})")


def _print_watch(out: dict[str, Any]) -> None:
    for ex in out.get("auto_exits", []):
        print(f"AUTO-EXIT: {ex.get('detail', '')}")
    trig = out.get("trigger")
    if trig:
        print(f"[trigger: {trig}] analisi eseguita.")
        a = out.get("analysis") or {}
        if a.get("ok"):
            print(f"Rationale: {a['decision'].get('rationale', '')}")
            for r in a.get("results", []):
                print(("  [x] " if r.get("executed") else "  [ ] ") + r.get("detail", ""))
    else:
        print("Nessun trigger: solo sorveglianza stop/take (nessuna analisi, costo zero).")
    s = out["state"]
    print(f"Account: €{s['value']:,.2f}  P&L {s['pnl_pct']:+.2f}%  "
          f"(cash €{s['cash_eur']:,.2f}, {s['n_positions']} posizioni)")


REPORT_TASK = """\
Scrivi un BREVE report mattutino in ITALIANO per il cliente sul book paper di \
cripto. Massimo 4-5 frasi, testo semplice (niente titoli markdown): \
(1) la lettura del regime sull'universo (RSI/MACD/trend, risk-on o risk-off), \
(2) come stanno andando le posizioni aperte e il rischio/livello chiave da \
seguire oggi, (3) una riga su cosa sei propenso a fare. Usa solo i numeri reali \
del contesto. Conciso, da leggere col caffè."""


def _report_commentary(market_md: str, account_md: str) -> str:
    context = (
        "=== CONTESTO REPORT MATTUTINO ===\n"
        f"{market_md}\n"
        "Book virtuale del cliente:\n"
        f"{account_md}\n"
        "=== FINE CONTESTO ==="
    )
    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=900,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=[
            {"type": "text", "text": prompts.stable_system_block(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context},
        ],
        messages=[{"role": "user", "content": REPORT_TASK}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


def _rome_now():
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:
        # Fallback if tz data is missing: approximate CEST (UTC+2).
        from datetime import timedelta
        return datetime.now(timezone.utc) + timedelta(hours=2)


def morning_report(*, notify_telegram: bool = True) -> dict[str, Any]:
    """Generate a daily morning report on the paper book and send it to Telegram."""
    rome = _rome_now()

    paper_broker.init_paper()
    snaps = btc_feed.get_universe_snapshot()
    prices = btc_feed.prices_from_snapshots(snaps)

    # Don't send a dataless report (and don't consume the daily guard) — let the
    # next cycle retry once market data is reachable again.
    if not prices:
        return {"ok": False, "reason": "no market data — report skipped, will retry next cycle"}

    state = paper_broker.get_state(prices)
    commentary = _report_commentary(btc_feed.format_universe(snaps),
                                    paper_broker.format_state(state))

    lines = [f"🌅 Report mattutino — {rome.strftime('%d/%m/%Y')}", ""]
    if state["value"] is not None:
        lines.append(f"Valore: €{state['value']:,.2f} | P&L {state['pnl_pct']:+.2f}%")
    lines.append(f"Cash: €{state['cash_eur']:,.2f} | {state['n_positions']} posizioni")
    for p in state["positions"]:
        pnl = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "—"
        lines.append(f"• {p['label']}: €{p['value']:,.0f} ({pnl})")
    if commentary:
        lines += ["", commentary]
    dash = os.environ.get("DASHBOARD_URL", "http://localhost:8501")
    lines += ["", f"📊 Dashboard: {dash}"]
    text = "\n".join(lines)

    if notify_telegram:
        send_telegram(text)
    paper_broker.set_meta("last_report_date", rome.strftime("%Y-%m-%d"))
    return {"ok": True, "text": text, "state": state}


def main() -> None:
    try:  # make console output robust to € / em-dash on Windows cp1252
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Multi-crypto paper-trading bot (virtual money).")
    ap.add_argument("--once", action="store_true", help="Run a single full cycle (analysis + trade).")
    ap.add_argument("--watch", action="store_true",
                    help="Lightweight watch: enforce stops/targets; analyze only on exit or (throttled) when flat.")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="Run every N seconds.")
    ap.add_argument("--telegram", action="store_true", help="Push each result to Telegram.")
    ap.add_argument("--reset", action="store_true", help="Reset the virtual account to €1000 and exit.")
    ap.add_argument("--report", action="store_true", help="Force-send the daily morning report and exit.")
    ap.add_argument("--report-if-due", action="store_true",
                    help="Send the morning report only if it's >=10:00 Rome and not already sent today.")
    args = ap.parse_args()

    if args.reset:
        paper_broker.reset()
        print(f"Virtual account reset to €{paper_broker.START_BALANCE_EUR:,.0f}.")
        return

    if args.report:
        out = morning_report(notify_telegram=True)
        print(out.get("text") or out.get("reason", "report skipped"))
        return

    if args.report_if_due:
        rome = _rome_now()
        today = rome.strftime("%Y-%m-%d")
        last = paper_broker.get_meta("last_report_date")
        if rome.hour >= 9 and last != today:
            out = morning_report(notify_telegram=True)
            if out.get("ok"):
                print(f"Report inviato (ora Roma {rome.hour}:00, data {today}).")
            else:
                print(f"Report NON inviato: {out.get('reason')}")
        else:
            print(f"Report non dovuto (ora Roma={rome.hour}, ultimo inviato={last}).")
        return

    if args.watch:
        _print_watch(watch_cycle())
        return

    if args.loop:
        print(f"Looping every {args.loop}s — Ctrl+C to stop.")
        try:
            while True:
                _print_cycle(run_cycle(notify_telegram=args.telegram))
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        _print_cycle(run_cycle(notify_telegram=args.telegram))


if __name__ == "__main__":
    main()
