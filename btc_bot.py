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
from typing import Any

import anthropic

import btc_feed
import paper_broker
import prompts
from telegram_alert import send_telegram

MODEL = "claude-opus-4-8"
MAX_POSITIONS = 4               # focused book — "not too many"
MAX_POSITION_PCT = 60.0         # cap any single position at 60% of equity
FULLY_INVESTED = False          # may invest up to 100%, but NOT forced — cash is allowed
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
                    "size_pct": {"type": "number", "description": "BUY/SHORT: % of free cash. SELL/COVER: % of that position. 0 for HOLD."},
                    "confidence": {"type": "number", "description": "0 to 1."},
                    "thesis": {"type": "string", "description": "Max 2 sentences."},
                    "stop_loss": {"type": ["number", "null"]},
                    "take_profit": {"type": ["number", "null"]},
                },
                "required": ["symbol", "action", "size_pct", "confidence", "thesis", "stop_loss", "take_profit"],
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
- BUY   = open/add a LONG (profit if price rises). size_pct = % of free cash.
- SELL  = reduce/close a LONG. size_pct = % of that long.
- SHORT = open/add a SHORT (profit if price FALLS). size_pct = % of free cash locked as collateral.
- COVER = reduce/close a SHORT. size_pct = % of that short.
- HOLD  = do nothing on that name.

Rules (philosophy + skills):
- NO LEVERAGE. Shorts are cash-collateralised 1:1, so (longs + short collateral) \
never exceeds your capital. A symbol is either long OR short — close it to flip.
- MULTI-TIMEFRAME: each asset is given on 4 windows (settimanale/weekly, \
giornaliera/daily, oraria/hourly, minuti/minute) with trend + RSI + MACD. Weight \
them by the trade's intended horizon — for the usual few-hours swing, weight \
HOURLY and DAILY most, use WEEKLY as the background-trend filter (don't trade \
against it) and MINUTE only to fine-tune entry. Enter when the higher-weighted \
windows AGREE; if they conflict, stay out.
- Read the regime with the indicators (RSI, MACD, Bollinger, EMA): if trend/ \
momentum is clearly DOWN, prefer SHORTS on the weakest names; if UP, prefer LONGS \
on the strongest; in chop, stay light and don't force trades.
- Hold 2–{MAX_POSITIONS} positions at a time, never more. Conviction over breadth. \
Deploy meaningfully — don't sit on large idle cash when there are clear setups.
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
  roughly ≥ 2× the stop distance); if the structure offers poor R:R, stay out.
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
        f"Constraints: max {MAX_POSITIONS} open positions; any single position "
        f"capped at {MAX_POSITION_PCT:.0f}% of account value.\n\n"
        "Recent trades:\n"
        f"{recent_md}\n"
        "=== END CONTEXT ==="
    )
    resp = _get_client().messages.create(
        model=MODEL,
        max_tokens=3500,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        system=[
            {"type": "text", "text": prompts.stable_system_block(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context},
        ],
        messages=[{"role": "user", "content": DECISION_TASK}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(text)


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
    used = {p["symbol"]: (p["collateral"] if p["side"] == "short" else p["value"])
            for p in state["positions"]}
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
            take_profit=o.get("take_profit"), source=snaps.get(tv, {}).get("source"))
        if res["executed"]:
            held.add(tv)
            used[tv] = used.get(tv, 0.0) + res["eur_amount"]
        results.append(res)

    return results


def _sweep_cash(prices: dict, snaps: dict) -> list[dict[str, Any]]:
    """
    Full-deployment policy: distribute any leftover cash across the currently
    held positions (proportional to remaining room under the per-position cap),
    so the book stays ~fully invested. Preserves each position's stop/target.
    """
    state = paper_broker.get_state(prices)
    cash, equity = state["cash_eur"], state["value"]
    longs = [p for p in state["positions"] if p["side"] == "long"]
    # If the book holds any short, respect the model's allocation — don't force-deploy.
    if cash < MIN_CASH_SWEEP or not longs or any(p["side"] == "short" for p in state["positions"]):
        return []

    cap_value = equity * MAX_POSITION_PCT / 100.0
    rooms = {p["symbol"]: (p, max(0.0, cap_value - p["value"])) for p in longs}
    total_room = sum(r for _, r in rooms.values())
    if total_room <= 0:
        return []

    results = []
    for sym, (p, room) in rooms.items():
        alloc = min(cash * room / total_room, room)
        if alloc < MIN_CASH_SWEEP:
            continue
        res = paper_broker.execute_order(
            symbol=sym, label=p["label"], action="BUY", size_pct=100.0,
            price=prices.get(sym, p["price"]), eur_cap=alloc, confidence=None,
            thesis="Full-deployment sweep — no idle cash.",
            source=snaps.get(sym, {}).get("source"),
        )
        if res["executed"]:
            results.append(res)
    return results


def run_cycle(*, notify_telegram: bool = False) -> dict[str, Any]:
    paper_broker.init_paper()
    snaps = btc_feed.get_universe_snapshot()
    prices = btc_feed.prices_from_snapshots(snaps)

    if not prices:
        return {"ok": False, "detail": "No market data available — skipped (no trade)."}

    auto_exits = paper_broker.check_exits(prices)
    state_before = paper_broker.get_state(prices)
    decision = decide(snaps, state_before)
    results = _execute_orders(decision, prices, snaps)
    if FULLY_INVESTED:
        results += _sweep_cash(prices, snaps)
    state = paper_broker.get_state(prices)

    # Record the value-vs-BTC time series (anchors the buy-&-hold benchmark).
    paper_broker.record_snapshot(state["value"], prices.get("BINANCE:BTCEUR"))

    out = {
        "ok": True, "snaps": snaps, "prices": prices, "auto_exits": auto_exits,
        "decision": decision, "results": results, "state": state,
    }
    if notify_telegram:
        send_telegram(_telegram_text(out))
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
    ap.add_argument("--once", action="store_true", help="Run a single cycle (default).")
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
        if rome.hour >= 10 and last != today:
            out = morning_report(notify_telegram=True)
            if out.get("ok"):
                print(f"Report inviato (ora Roma {rome.hour}:00, data {today}).")
            else:
                print(f"Report NON inviato: {out.get('reason')}")
        else:
            print(f"Report non dovuto (ora Roma={rome.hour}, ultimo inviato={last}).")
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
