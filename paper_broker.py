"""
paper_broker.py — Multi-asset virtual (paper) trading account, LONG and SHORT.

100% simulated: one EUR cash pool plus positions in several crypto symbols.
Starts with virtual cash (default €1000). No real money, no exchange, no orders.

Shorts are **cash-collateralised, no leverage**: opening a short locks an equal
amount of cash as collateral, so (longs value + shorts collateral) can never
exceed your capital. Short P&L = (entry_price - current_price) * qty.

Positions are keyed by the TradingView symbol (e.g. "BINANCE:BTCEUR") and carry
a `side` of 'long' or 'short' (a symbol can't be both at once — close to flip).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from memory import DB_PATH

START_BALANCE_EUR = 1000.0
MIN_TRADE_EUR = 5.0


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
                side         TEXT NOT NULL DEFAULT 'long',   -- 'long' | 'short'
                qty          REAL NOT NULL,
                avg_cost     REAL,                            -- entry price (long avg / short entry)
                collateral   REAL NOT NULL DEFAULT 0,         -- cash locked for shorts
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
                action       TEXT NOT NULL,    -- BUY | SELL | SHORT | COVER | HOLD
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                account_value REAL, btc_price REAL)"""
        )
        conn.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT)")

        # Migrations for older DBs.
        acc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_account)")}
        if "bench_start_price" not in acc_cols:
            conn.execute("ALTER TABLE paper_account ADD COLUMN bench_start_price REAL")
        if "bench_btc_qty" not in acc_cols:
            conn.execute("ALTER TABLE paper_account ADD COLUMN bench_btc_qty REAL")
        pos_cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_positions)")}
        if "side" not in pos_cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN side TEXT NOT NULL DEFAULT 'long'")
        if "collateral" not in pos_cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN collateral REAL NOT NULL DEFAULT 0")

        if conn.execute("SELECT id FROM paper_account WHERE id = 1").fetchone() is None:
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
        return dict(conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone())


def get_positions() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE qty > 1e-9 ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


def get_position(symbol: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)).fetchone()
    return dict(row) if row and row["qty"] > 1e-9 else (dict(row) if row else None)


def _position_value(p: dict[str, Any], price: float | None) -> tuple[float, float | None, float | None]:
    """Return (value, pnl, pnl_pct) for a position at `price`."""
    px = price if price is not None else p.get("last_price")
    if not px:
        return 0.0, None, None
    if p["side"] == "short":
        pnl = (p["avg_cost"] - px) * p["qty"] if p.get("avg_cost") else None
        coll = p.get("collateral") or 0.0
        value = coll + (pnl or 0.0)
        pnl_pct = (pnl / coll * 100) if (pnl is not None and coll) else None
        return value, pnl, pnl_pct
    value = px * p["qty"]
    pnl = pnl_pct = None
    if p.get("avg_cost"):
        cost = p["avg_cost"] * p["qty"]
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else None
    return value, pnl, pnl_pct


def get_state(prices: dict[str, float] | None = None) -> dict[str, Any]:
    prices = prices or {}
    acct = get_account()
    positions, invested = [], 0.0
    for p in get_positions():
        px = prices.get(p["symbol"], p.get("last_price"))
        value, pnl, pnl_pct = _position_value(p, px)
        invested += value
        positions.append({
            "symbol": p["symbol"], "label": p.get("label") or p["symbol"], "side": p["side"],
            "qty": p["qty"], "avg_cost": p.get("avg_cost"), "price": px, "value": value,
            "pnl": pnl, "pnl_pct": pnl_pct, "collateral": p.get("collateral") or 0.0,
            "active_stop": p.get("active_stop"), "active_take": p.get("active_take"),
        })
    value = acct["cash_eur"] + invested
    pnl = value - acct["start_balance"]
    pnl_pct = (pnl / acct["start_balance"] * 100) if acct["start_balance"] else None
    return {
        "cash_eur": acct["cash_eur"], "start_balance": acct["start_balance"],
        "invested": invested, "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
        "n_positions": len(positions), "positions": positions,
    }


# ------------------------------------------------------------------- writes

def _set_cash(cash: float) -> None:
    with _conn() as conn:
        conn.execute("UPDATE paper_account SET cash_eur = ?, updated_at = ? WHERE id = 1", (cash, _now()))


def _upsert_position(symbol, label, side, qty, avg_cost, collateral, stop, take, price) -> None:
    with _conn() as conn:
        if qty <= 1e-9:
            conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (symbol,))
        else:
            conn.execute(
                """
                INSERT INTO paper_positions
                  (symbol, label, side, qty, avg_cost, collateral, active_stop, active_take, last_price, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                  label=excluded.label, side=excluded.side, qty=excluded.qty,
                  avg_cost=excluded.avg_cost, collateral=excluded.collateral,
                  active_stop=excluded.active_stop, active_take=excluded.active_take,
                  last_price=excluded.last_price, updated_at=excluded.updated_at
                """,
                (symbol, label, side, qty, avg_cost, collateral, stop, take, price, _now()),
            )


def _record_trade(**kw: Any) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO paper_trades
              (ts, symbol, label, action, price, qty, eur_amount, cash_after,
               confidence, thesis, stop_loss, take_profit, source)
            VALUES (:ts, :symbol, :label, :action, :price, :qty, :eur_amount,
                    :cash_after, :confidence, :thesis, :stop_loss, :take_profit, :source)""",
            kw,
        )


def execute_order(
    *, symbol: str, action: str, size_pct: float, price: float, label: str | None = None,
    eur_cap: float | None = None, confidence: float | None = None, thesis: str | None = None,
    stop_loss: float | None = None, take_profit: float | None = None, source: str | None = None,
) -> dict[str, Any]:
    """
    BUY   → open/add a LONG using size_pct% of free cash (optionally capped).
    SELL  → reduce the LONG by size_pct% of its quantity.
    SHORT → open/add a SHORT, locking size_pct% of free cash as collateral.
    COVER → buy back size_pct% of the SHORT quantity (realises P&L).
    """
    action = (action or "HOLD").upper()
    size_pct = max(0.0, min(100.0, float(size_pct or 0)))
    acct = get_account()
    cash = acct["cash_eur"]
    pos = get_position(symbol)
    side = pos["side"] if pos else None
    label = label or (pos.get("label") if pos else symbol)
    executed, detail, qty, eur_amount = False, "HOLD.", 0.0, 0.0

    if action == "BUY":
        if side == "short":
            detail = f"BUY {label} ignorato — esiste una SHORT aperta (prima COVER)."
            action = "HOLD"
        else:
            eur_amount = cash * size_pct / 100.0
            if eur_cap is not None:
                eur_amount = min(eur_amount, eur_cap)
            eur_amount = min(eur_amount, cash)
            if eur_amount < MIN_TRADE_EUR:
                detail, action = f"BUY {label} skipped — €{eur_amount:,.2f} sotto minimo.", "HOLD"
            else:
                qty = eur_amount / price
                held = pos["qty"] if pos else 0.0
                new_qty = held + qty
                avg = (held * pos["avg_cost"] + qty * price) / new_qty if (pos and pos.get("avg_cost")) else price
                cash -= eur_amount
                _set_cash(cash)
                _upsert_position(symbol, label, "long", new_qty, avg, 0.0,
                                 stop_loss or (pos.get("active_stop") if pos else None),
                                 take_profit or (pos.get("active_take") if pos else None), price)
                executed = True
                detail = f"BUY {label}: €{eur_amount:,.2f} → {qty:.6f} @ €{price:,.2f}."

    elif action == "SELL":
        held = pos["qty"] if (pos and side == "long") else 0.0
        qty = held * size_pct / 100.0
        eur_amount = qty * price
        if held <= 1e-9 or eur_amount < MIN_TRADE_EUR:
            detail, action = f"SELL {label} skipped — niente long da vendere.", "HOLD"
        else:
            new_qty = held - qty
            cash += eur_amount
            _set_cash(cash)
            keep = new_qty > 1e-9
            _upsert_position(symbol, label, "long", new_qty, pos.get("avg_cost"), 0.0,
                             pos.get("active_stop") if keep else None,
                             pos.get("active_take") if keep else None, price)
            executed = True
            detail = f"SELL {label}: {qty:.6f} → €{eur_amount:,.2f} @ €{price:,.2f}."

    elif action == "SHORT":
        if side == "long":
            detail, action = f"SHORT {label} ignorato — esiste una LONG aperta (prima SELL).", "HOLD"
        else:
            eur_amount = cash * size_pct / 100.0   # collateral
            if eur_cap is not None:
                eur_amount = min(eur_amount, eur_cap)
            eur_amount = min(eur_amount, cash)
            if eur_amount < MIN_TRADE_EUR:
                detail, action = f"SHORT {label} skipped — €{eur_amount:,.2f} sotto minimo.", "HOLD"
            else:
                qty = eur_amount / price
                held = pos["qty"] if pos else 0.0
                new_qty = held + qty
                entry = (held * pos["avg_cost"] + qty * price) / new_qty if (pos and pos.get("avg_cost")) else price
                new_coll = (pos.get("collateral") or 0.0 if pos else 0.0) + eur_amount
                cash -= eur_amount
                _set_cash(cash)
                _upsert_position(symbol, label, "short", new_qty, entry, new_coll,
                                 stop_loss or (pos.get("active_stop") if pos else None),
                                 take_profit or (pos.get("active_take") if pos else None), price)
                executed = True
                detail = f"SHORT {label}: collat €{eur_amount:,.2f} → {qty:.6f} @ €{price:,.2f}."

    elif action == "COVER":
        held = pos["qty"] if (pos and side == "short") else 0.0
        qty = held * size_pct / 100.0
        if held <= 1e-9 or qty * price < MIN_TRADE_EUR:
            detail, action = f"COVER {label} skipped — niente short da coprire.", "HOLD"
        else:
            entry = pos.get("avg_cost") or price
            coll = pos.get("collateral") or 0.0
            coll_release = coll * (qty / held)
            pnl = (entry - price) * qty
            cash += coll_release + pnl
            _set_cash(cash)
            new_qty = held - qty
            keep = new_qty > 1e-9
            _upsert_position(symbol, label, "short", new_qty, entry, coll - coll_release,
                             pos.get("active_stop") if keep else None,
                             pos.get("active_take") if keep else None, price)
            executed = True
            eur_amount = qty * price
            detail = f"COVER {label}: {qty:.6f} @ €{price:,.2f} (P&L €{pnl:,.2f})."
    else:
        if pos:
            _upsert_position(symbol, label, pos["side"], pos["qty"], pos.get("avg_cost"),
                             pos.get("collateral") or 0.0, pos.get("active_stop"),
                             pos.get("active_take"), price)

    _record_trade(ts=_now(), symbol=symbol, label=label, action=action, price=price,
                  qty=qty if executed else 0.0, eur_amount=eur_amount if executed else 0.0,
                  cash_after=cash, confidence=confidence, thesis=thesis,
                  stop_loss=stop_loss, take_profit=take_profit, source=source)
    return {"executed": executed, "symbol": symbol, "label": label, "action": action,
            "detail": detail, "qty": qty, "eur_amount": eur_amount}


def intrabar_exit_decision(p: dict[str, Any], candles: list[tuple[int, float, float]]) -> dict[str, Any] | None:
    """
    Did the price touch this position's stop/target BETWEEN cycles? Scans the
    (ts_ms, high, low) candles after the position's last update, in time order,
    and returns the level to fill at (as a resting order would) — or None.
    Stop has priority over target within the same candle (conservative).
    """
    stop, take, side = p.get("active_stop"), p.get("active_take"), p["side"]
    if not (stop or take) or not candles:
        return None
    try:
        last_ts = datetime.fromisoformat(p["updated_at"]).timestamp() * 1000
    except Exception:
        last_ts = 0
    for ts, hi, lo in candles:
        if ts <= last_ts:
            continue
        if side == "long":
            if stop and lo <= stop:
                return {"level": stop, "reason": f"STOP-LOSS {p['label']} (long) toccato a €{stop:,.2f} tra i cicli"}
            if take and hi >= take:
                return {"level": take, "reason": f"TAKE-PROFIT {p['label']} (long) toccato a €{take:,.2f} tra i cicli"}
        else:  # short
            if stop and hi >= stop:
                return {"level": stop, "reason": f"STOP-LOSS {p['label']} (short) toccato a €{stop:,.2f} tra i cicli"}
            if take and lo <= take:
                return {"level": take, "reason": f"TAKE-PROFIT {p['label']} (short) toccato a €{take:,.2f} tra i cicli"}
    return None


def check_exits(prices: dict[str, float]) -> list[dict[str, Any]]:
    """
    Force-close positions whose live price breaches their stop / target.
    Fills at the LEVEL (stop/take), not the current price — like a resting order —
    so take-profits are capped at the level and never over-counted.
    """
    results = []
    for p in get_positions():
        price = prices.get(p["symbol"], p.get("last_price"))
        if not price:
            continue
        stop, take = p.get("active_stop"), p.get("active_take")
        reason = close_action = fill = None
        if p["side"] == "long":
            if stop and price <= stop:
                reason, close_action, fill = f"STOP-LOSS {p['label']} (long) @ €{stop:,.2f}", "SELL", stop
            elif take and price >= take:
                reason, close_action, fill = f"TAKE-PROFIT {p['label']} (long) @ €{take:,.2f}", "SELL", take
        else:  # short
            if stop and price >= stop:
                reason, close_action, fill = f"STOP-LOSS {p['label']} (short) @ €{stop:,.2f}", "COVER", stop
            elif take and price <= take:
                reason, close_action, fill = f"TAKE-PROFIT {p['label']} (short) @ €{take:,.2f}", "COVER", take
        if reason:
            res = execute_order(symbol=p["symbol"], label=p.get("label"), action=close_action,
                                size_pct=100.0, price=fill, confidence=1.0, thesis=reason, source="auto-exit")
            res["exit_reason"] = reason
            results.append(res)
    return results


# ---------------------------------------------------------------- meta / benchmark

def get_meta(key: str) -> str | None:
    init_paper()
    with _conn() as conn:
        row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_benchmark() -> dict[str, Any]:
    acct = get_account()
    return {"start_price": acct.get("bench_start_price"), "btc_qty": acct.get("bench_btc_qty")}


def ensure_benchmark(btc_price: float | None) -> None:
    if not btc_price:
        return
    acct = get_account()
    if not acct.get("bench_start_price"):
        qty = acct["start_balance"] / btc_price
        with _conn() as conn:
            conn.execute("UPDATE paper_account SET bench_start_price = ?, bench_btc_qty = ?, "
                         "updated_at = ? WHERE id = 1", (btc_price, qty, _now()))


def benchmark_value(btc_price: float | None) -> float | None:
    b = get_benchmark()
    return b["btc_qty"] * btc_price if (b["btc_qty"] and btc_price) else None


def record_snapshot(account_value: float | None, btc_price: float | None) -> None:
    ensure_benchmark(btc_price)
    with _conn() as conn:
        conn.execute("INSERT INTO bot_snapshots (ts, account_value, btc_price) VALUES (?, ?, ?)",
                     (_now(), account_value, btc_price))


def get_bot_snapshots(limit: int = 1000) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT ts, account_value, btc_price FROM bot_snapshots "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_trades(limit: int = 30) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def reset(start_balance: float = START_BALANCE_EUR) -> None:
    init_paper(start_balance)  # ensure tables exist
    with _conn() as conn:
        for t in ("paper_trades", "paper_positions", "bot_snapshots", "bot_meta", "paper_account"):
            conn.execute(f"DELETE FROM {t}")
    init_paper(start_balance)  # re-seed the account row


# ------------------------------------------------------------------- format

def format_state(state: dict[str, Any]) -> str:
    lines = [
        f"- Virtual cash (free): €{state['cash_eur']:,.2f}",
        f"- Invested/at risk: €{state['invested']:,.2f} across {state['n_positions']} positions",
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
                f"   · {p['side'].upper()} {p['label']}: {p['qty']:.6f} @ €{(p['avg_cost'] or 0):,.2f} "
                f"→ €{p['value']:,.2f} ({pnl}){stp}{tkp}")
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
