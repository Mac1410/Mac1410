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
FULLY_INVESTED = True           # deploy ~all cash; don't sit on idle liquidity
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
                    "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "size_pct": {"type": "number", "description": "BUY: % of available cash. SELL: % of that position. 0 for HOLD."},
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
You actively manage a VIRTUAL multi-crypto paper account (EUR). This is an active \
experiment — do NOT sit 100% in cash. Build and maintain a FOCUSED book of \
high-conviction crypto positions and rotate as theses change.

Universe you may trade: {", ".join(_LABELS)}.

Rules (from the client's philosophy + project skills):
- STAY FULLY INVESTED. The client does not want idle cash — deploy ~all of it \
across the book. Target roughly 0% cash. Any leftover cash is auto-swept into \
your chosen positions, so allocate as if every euro must be working.
- Conviction over diversification: hold 2–{MAX_POSITIONS} positions at a time — \
never more than {MAX_POSITIONS}, and at least 2 so you're not all-in on one name. \
A few strong ideas beat many weak ones.
- Use the live indicators (RSI, MACD, Bollinger, EMA) to judge momentum, trend, \
and over-bought/over-sold — rank the strongest relative setups and weight toward them.
- Every BUY needs a one-to-two sentence thesis AND a pre-committed stop_loss \
(and ideally a take_profit), in EUR price terms.
- Rotate, don't hoard cash: trim/exit weak names on thesis break and redeploy that \
capital into the strongest setups. Being in cash is only acceptable transiently \
(e.g. just after a stop-out) until you re-enter.
- No leverage: you can only deploy the cash you have; never more than 100% invested.

Return a set of orders (one per symbol you want to act on). size_pct for BUY = \
percent of CURRENT cash to deploy into that name. Respond strictly in the required \
JSON schema."""


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
    """Apply orders: SELLs first (free cash), then BUYs with caps."""
    state = paper_broker.get_state(prices)
    equity = state["value"]
    held = {p["symbol"] for p in state["positions"]}
    held_value = {p["symbol"]: p["value"] for p in state["positions"]}

    orders = decision.get("orders", [])
    sells = [o for o in orders if o["action"] == "SELL"]
    buys = [o for o in orders if o["action"] == "BUY"]
    results: list[dict[str, Any]] = []

    for o in sells:
        tv = _LABEL_TO_TV.get(o["symbol"])
        if not tv or tv not in prices:
            continue
        res = paper_broker.execute_order(
            symbol=tv, label=o["symbol"], action="SELL", size_pct=o["size_pct"],
            price=prices[tv], confidence=o.get("confidence"), thesis=o.get("thesis"),
            source=snaps.get(tv, {}).get("source"),
        )
        if res["executed"]:
            held.discard(tv)
            results.append(res)

    for o in buys:
        tv = _LABEL_TO_TV.get(o["symbol"])
        if not tv or tv not in prices:
            continue
        # Enforce the max-positions cap for *new* names.
        if tv not in held and len(held) >= MAX_POSITIONS:
            results.append({"executed": False, "label": o["symbol"], "action": "BUY",
                            "detail": f"BUY {o['symbol']} skipped — max {MAX_POSITIONS} positions reached."})
            continue
        # Cap any single position at MAX_POSITION_PCT of equity.
        cap_value = equity * MAX_POSITION_PCT / 100.0
        eur_cap = max(0.0, cap_value - held_value.get(tv, 0.0))
        res = paper_broker.execute_order(
            symbol=tv, label=o["symbol"], action="BUY", size_pct=o["size_pct"],
            price=prices[tv], eur_cap=eur_cap, confidence=o.get("confidence"),
            thesis=o.get("thesis"), stop_loss=o.get("stop_loss"),
            take_profit=o.get("take_profit"), source=snaps.get(tv, {}).get("source"),
        )
        if res["executed"]:
            held.add(tv)
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
    if cash < MIN_CASH_SWEEP or not state["positions"]:
        return []

    cap_value = equity * MAX_POSITION_PCT / 100.0
    rooms = {p["symbol"]: (p, max(0.0, cap_value - p["value"])) for p in state["positions"]}
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


def morning_report(*, notify_telegram: bool = True) -> dict[str, Any]:
    """Generate a daily morning report on the paper book and send it to Telegram."""
    from datetime import date

    paper_broker.init_paper()
    snaps = btc_feed.get_universe_snapshot()
    prices = btc_feed.prices_from_snapshots(snaps)
    state = paper_broker.get_state(prices)
    commentary = _report_commentary(btc_feed.format_universe(snaps),
                                    paper_broker.format_state(state))

    lines = [f"🌅 Report mattutino — {date.today().strftime('%d/%m/%Y')}", ""]
    if state["value"] is not None:
        lines.append(f"Valore: €{state['value']:,.2f} | P&L {state['pnl_pct']:+.2f}%")
    lines.append(f"Cash: €{state['cash_eur']:,.2f} | {state['n_positions']} posizioni")
    for p in state["positions"]:
        pnl = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "—"
        lines.append(f"• {p['label']}: €{p['value']:,.0f} ({pnl})")
    if commentary:
        lines += ["", commentary]
    text = "\n".join(lines)

    if notify_telegram:
        send_telegram(text)
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
    ap.add_argument("--report", action="store_true", help="Send the daily morning report to Telegram and exit.")
    args = ap.parse_args()

    if args.reset:
        paper_broker.reset()
        print(f"Virtual account reset to €{paper_broker.START_BALANCE_EUR:,.0f}.")
        return

    if args.report:
        out = morning_report(notify_telegram=True)
        print(out["text"])
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
