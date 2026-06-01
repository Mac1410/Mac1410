"""
paper_broker.py — Multi-asset virtual (paper) trading account.

100% simulated: one EUR cash pool plus positions in several crypto symbols.
Starts with virtual cash (default €1000). No real money, no exchange, no orders.

Positions are keyed by the TradingView symbol (e.g. "BINANCE:BTCEUR").
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from memory import DB_PATH

START_BALANCE_EUR = 1000.0
MIN_TRADE_EUR = 5.0  # ignore dust-sized simulated orders


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_paper(start_balance: float = START_BALANCE_EUR) -> None:
    """Create paper-trading tables and seed the cash account if empty."""
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                cash_eur      REAL NOT NULL,
                start_balance REAL NOT NULL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                symbol       TEXT PRIMARY KEY,
                label        TEXT,
                qty          REAL NOT NULL,
                avg_cost     REAL,
                active_stop  REAL,
                active_take  REAL,
                last_price   REAL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                symbol       TEXT,
                label        TEXT,
                action       TEXT NOT NULL,
                price        REAL,
                qty          REAL,
                eur_amount   REAL,
                cash_after   REAL,
                confidence   REAL,
                thesis       TEXT,
                stop_loss    REAL,
                take_profit  REAL,
                source       TEXT
            );
            """
        )
        row = conn.execute("SELECT id FROM paper_account WHERE id = 1").fetchone()
        if row is None:
            now = _now()
            conn.execute(
                "INSERT INTO paper_account (id, cash_eur, start_balance, created_at, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (start_balance, start_balance, now, now),
            )


# ------------------------------------------------------------------- reads

def get_account() -> dict[str, Any]:
    init_paper()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
    return dict(row)


def get_positions() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE qty > 1e-9 ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


def get_position(symbol: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)).fetchone()
    return dict(row) if row else None


def get_state(prices: dict[str, float] | None = None) -> dict[str, Any]:
    """
    Full account state. `prices` maps symbol -> current price; falls back to each
    position's last stored price when a live price is missing.
    """
    prices = prices or {}
    acct = get_account()
    positions = []
    invested = 0.0
    for p in get_positions():
        px = prices.get(p["symbol"], p.get("last_price"))
        value = (px or 0) * p["qty"]
        invested += value
        pnl = pnl_pct = None
        if px and p.get("avg_cost"):
            cost = p["avg_cost"] * p["qty"]
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost else None
        positions.append({
            "symbol": p["symbol"], "label": p.get("label") or p["symbol"],
            "qty": p["qty"], "avg_cost": p.get("avg_cost"), "price": px,
            "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
            "active_stop": p.get("active_stop"), "active_take": p.get("active_take"),
        })
    value = acct["cash_eur"] + invested
    pnl = value - acct["start_balance"]
    pnl_pct = (pnl / acct["start_balance"] * 100) if acct["start_balance"] else None
    return {
        "cash_eur": acct["cash_eur"],
        "start_balance": acct["start_balance"],
        "invested": invested,
        "value": value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "n_positions": len(positions),
        "positions": positions,
    }


# ------------------------------------------------------------------- writes

def _set_cash(cash: float) -> None:
    with _conn() as conn:
        conn.execute("UPDATE paper_account SET cash_eur = ?, updated_at = ? WHERE id = 1",
                     (cash, _now()))


def _upsert_position(symbol, label, qty, avg_cost, stop, take, price) -> None:
    with _conn() as conn:
        if qty <= 1e-9:
            conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (symbol,))
        else:
            conn.execute(
                """
                INSERT INTO paper_positions
                  (symbol, label, qty, avg_cost, active_stop, active_take, last_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                  label=excluded.label, qty=excluded.qty, avg_cost=excluded.avg_cost,
                  active_stop=excluded.active_stop, active_take=excluded.active_take,
                  last_price=excluded.last_price, updated_at=excluded.updated_at
                """,
                (symbol, label, qty, avg_cost, stop, take, price, _now()),
            )


def _record_trade(**kw: Any) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades
              (ts, symbol, label, action, price, qty, eur_amount, cash_after,
               confidence, thesis, stop_loss, take_profit, source)
            VALUES (:ts, :symbol, :label, :action, :price, :qty, :eur_amount,
                    :cash_after, :confidence, :thesis, :stop_loss, :take_profit, :source)
            """,
            kw,
        )


def execute_order(
    *,
    symbol: str,
    action: str,
    size_pct: float,
    price: float,
    label: str | None = None,
    eur_cap: float | None = None,
    confidence: float | None = None,
    thesis: str | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """
    Apply one order for one symbol.

    BUY  → spend `size_pct`% of available cash (optionally capped at `eur_cap`).
    SELL → sell `size_pct`% of the held quantity for that symbol.
    """
    action = (action or "HOLD").upper()
    size_pct = max(0.0, min(100.0, float(size_pct or 0)))
    acct = get_account()
    cash = acct["cash_eur"]
    pos = get_position(symbol)
    held_qty = pos["qty"] if pos else 0.0
    avg_cost = pos.get("avg_cost") if pos else None
    cur_stop = pos.get("active_stop") if pos else None
    cur_take = pos.get("active_take") if pos else None
    label = label or (pos.get("label") if pos else symbol)

    executed = False
    detail = "HOLD."
    qty = eur_amount = 0.0

    if action == "BUY":
        eur_amount = cash * size_pct / 100.0
        if eur_cap is not None:
            eur_amount = min(eur_amount, eur_cap)
        eur_amount = min(eur_amount, cash)
        if eur_amount < MIN_TRADE_EUR:
            detail = f"BUY {label} skipped — €{eur_amount:,.2f} below minimum."
            action = "HOLD"
        else:
            qty = eur_amount / price
            new_qty = held_qty + qty
            # weighted average cost
            if held_qty > 0 and avg_cost:
                avg_cost = (held_qty * avg_cost + qty * price) / new_qty
            else:
                avg_cost = price
            cash -= eur_amount
            _set_cash(cash)
            _upsert_position(symbol, label, new_qty, avg_cost,
                             stop_loss if stop_loss else cur_stop,
                             take_profit if take_profit else cur_take, price)
            executed = True
            detail = f"BUY {label}: €{eur_amount:,.2f} → {qty:.6f} @ €{price:,.2f}."

    elif action == "SELL":
        qty = held_qty * size_pct / 100.0
        eur_amount = qty * price
        if held_qty <= 1e-9 or eur_amount < MIN_TRADE_EUR:
            detail = f"SELL {label} skipped — nothing meaningful to sell."
            action = "HOLD"
        else:
            new_qty = held_qty - qty
            cash += eur_amount
            _set_cash(cash)
            keep_stop = cur_stop if new_qty > 1e-9 else None
            keep_take = cur_take if new_qty > 1e-9 else None
            _upsert_position(symbol, label, new_qty, avg_cost, keep_stop, keep_take, price)
            executed = True
            detail = f"SELL {label}: {qty:.6f} → €{eur_amount:,.2f} @ €{price:,.2f}."
    else:
        # Update last price for valuation even on HOLD.
        if pos:
            _upsert_position(symbol, label, held_qty, avg_cost, cur_stop, cur_take, price)

    _record_trade(
        ts=_now(), symbol=symbol, label=label, action=action, price=price,
        qty=qty if executed else 0.0, eur_amount=eur_amount if executed else 0.0,
        cash_after=cash, confidence=confidence, thesis=thesis,
        stop_loss=stop_loss, take_profit=take_profit, source=source,
    )
    return {"executed": executed, "symbol": symbol, "label": label,
            "action": action, "detail": detail, "qty": qty, "eur_amount": eur_amount}


def check_exits(prices: dict[str, float]) -> list[dict[str, Any]]:
    """Force-close any position whose live price breaches its stop/target."""
    results = []
    for pos in get_positions():
        price = prices.get(pos["symbol"], pos.get("last_price"))
        if not price:
            continue
        stop, take = pos.get("active_stop"), pos.get("active_take")
        reason = None
        if stop and price <= stop:
            reason = f"STOP-LOSS {pos['label']} — €{price:,.2f} ≤ €{stop:,.2f}"
        elif take and price >= take:
            reason = f"TAKE-PROFIT {pos['label']} — €{price:,.2f} ≥ €{take:,.2f}"
        if reason:
            res = execute_order(
                symbol=pos["symbol"], label=pos.get("label"), action="SELL",
                size_pct=100.0, price=price, confidence=1.0,
                thesis=reason, source="auto-exit",
            )
            res["exit_reason"] = reason
            results.append(res)
    return results


def get_trades(limit: int = 30) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def reset(start_balance: float = START_BALANCE_EUR) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM paper_positions")
        conn.execute("DELETE FROM paper_account")
    init_paper(start_balance)


# ------------------------------------------------------------------- format

def format_state(state: dict[str, Any]) -> str:
    lines = [
        f"- Virtual cash: €{state['cash_eur']:,.2f}",
        f"- Invested: €{state['invested']:,.2f} across {state['n_positions']} positions",
        f"- Account value: €{state['value']:,.2f}",
        f"- Total P&L: €{state['pnl']:,.2f} ({state['pnl_pct']:+.2f}%)",
        f"- Starting balance: €{state['start_balance']:,.2f}",
    ]
    if state["positions"]:
        lines.append("- Open positions:")
        for p in state["positions"]:
            pnl = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "—"
            stp = f" stop €{p['active_stop']:,.0f}" if p["active_stop"] else ""
            tkp = f" target €{p['active_take']:,.0f}" if p["active_take"] else ""
            lines.append(
                f"   · {p['label']}: {p['qty']:.6f} @ avg €{(p['avg_cost'] or 0):,.2f} "
                f"→ €{p['value']:,.2f} ({pnl}){stp}{tkp}"
            )
    else:
        lines.append("- Open positions: none (100% cash)")
    return "\n".join(lines)


def format_recent_trades(limit: int = 6) -> str:
    trades = [t for t in get_trades(limit) if t["action"] != "HOLD"]
    if not trades:
        return "No prior trades yet."
    out = []
    for t in trades:
        px = f"€{t['price']:,.2f}" if t["price"] else "—"
        out.append(f"- [{t['ts'][:16]}] {t['action']} {t.get('label') or t['symbol']} @ {px} — {t['thesis'] or ''}")
    return "\n".join(out)
