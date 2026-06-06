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
# Auto-cap leverage so liquidation sits at least this multiple of the stop distance
# beyond entry → the technical stop always fills before liquidation (no premature
# margin wipeout). Higher = safer but lower effective leverage.
LIQUIDATION_STOP_BUFFER = 1.5


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

        # One-time: leverage uses `collateral` as the locked MARGIN for longs too.
        # Pre-leverage longs stored collateral=0 (margin == notional == avg_cost*qty);
        # backfill so value/liquidation maths stay correct for legacy positions.
        if conn.execute("SELECT value FROM bot_meta WHERE key = 'lev_margin_backfill'").fetchone() is None:
            conn.execute(
                "UPDATE paper_positions SET collateral = avg_cost * qty "
                "WHERE side = 'long' AND (collateral IS NULL OR collateral = 0) "
                "AND avg_cost IS NOT NULL AND qty > 0"
            )
            conn.execute("INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('lev_margin_backfill', '1')")


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
    """
    Return (value, pnl, pnl_pct) for a position at `price` — leverage-aware.
    Margin (locked cash) is stored in `collateral` for BOTH sides; P&L accrues on
    the full leveraged quantity. value = margin + pnl; pnl_pct is on the margin.
    """
    px = price if price is not None else p.get("last_price")
    if not px:
        return 0.0, None, None
    margin = p.get("collateral") or 0.0
    entry = p.get("avg_cost")
    qty = p["qty"]
    if entry:
        pnl = (entry - px) * qty if p["side"] == "short" else (px - entry) * qty
    else:
        pnl = None
    value = margin + (pnl or 0.0)
    pnl_pct = (pnl / margin * 100) if (pnl is not None and margin) else None
    return value, pnl, pnl_pct


def get_state(prices: dict[str, float] | None = None) -> dict[str, Any]:
    prices = prices or {}
    acct = get_account()
    positions, invested = [], 0.0
    for p in get_positions():
        px = prices.get(p["symbol"], p.get("last_price"))
        value, pnl, pnl_pct = _position_value(p, px)
        invested += value
        margin = p.get("collateral") or 0.0
        lev = (p["qty"] * p["avg_cost"] / margin) if (margin and p.get("avg_cost")) else 1.0
        positions.append({
            "symbol": p["symbol"], "label": p.get("label") or p["symbol"], "side": p["side"],
            "qty": p["qty"], "avg_cost": p.get("avg_cost"), "price": px, "value": value,
            "pnl": pnl, "pnl_pct": pnl_pct, "collateral": margin, "leverage": lev,
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


def _cap_leverage_to_stop(lev: float, entry: float, stop: float | None) -> float:
    """
    Lower leverage so liquidation sits at least LIQUIDATION_STOP_BUFFER × the stop
    distance beyond entry — guarantees the chosen technical stop fills BEFORE
    liquidation (no premature margin wipeout). The stop is left untouched; only the
    leverage is reduced. Returns the (possibly reduced) leverage, never below 1.
    """
    if not stop or not entry or entry <= 0:
        return lev
    d_stop = abs(entry - stop) / entry
    if d_stop <= 0:
        return lev
    lev_max = 1.0 / (LIQUIDATION_STOP_BUFFER * d_stop)
    return max(1.0, min(lev, lev_max))


def execute_order(
    *, symbol: str, action: str, size_pct: float, price: float, label: str | None = None,
    eur_cap: float | None = None, confidence: float | None = None, thesis: str | None = None,
    stop_loss: float | None = None, take_profit: float | None = None, source: str | None = None,
    leverage: float = 1.0,
) -> dict[str, Any]:
    """
    BUY   → open/add a LONG; size_pct% of free cash is the MARGIN, exposure = margin × leverage.
    SELL  → reduce the LONG by size_pct% of its quantity (releases margin + realises P&L).
    SHORT → open/add a SHORT; size_pct% of free cash is the MARGIN, exposure = margin × leverage.
    COVER → buy back size_pct% of the SHORT quantity (releases margin + realises P&L).
    """
    action = (action or "HOLD").upper()
    size_pct = max(0.0, min(100.0, float(size_pct or 0)))
    lev = max(1.0, min(5.0, float(leverage or 1)))
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
            margin = cash * size_pct / 100.0
            if eur_cap is not None:
                margin = min(margin, eur_cap)
            margin = min(margin, cash)
            if margin < MIN_TRADE_EUR:
                detail, action = f"BUY {label} skipped — €{margin:,.2f} di margine sotto minimo.", "HOLD"
            else:
                eff_stop = stop_loss if stop_loss is not None else (pos.get("active_stop") if pos else None)
                lev_use = _cap_leverage_to_stop(lev, price, eff_stop)
                notional = margin * lev_use
                qty = notional / price
                held = pos["qty"] if pos else 0.0
                new_qty = held + qty
                avg = (held * pos["avg_cost"] + qty * price) / new_qty if (pos and pos.get("avg_cost")) else price
                new_margin = (pos.get("collateral") or 0.0 if pos else 0.0) + margin
                cash -= margin
                _set_cash(cash)
                _upsert_position(symbol, label, "long", new_qty, avg, new_margin,
                                 stop_loss or (pos.get("active_stop") if pos else None),
                                 take_profit or (pos.get("active_take") if pos else None), price)
                executed = True
                eur_amount = margin
                eff_lev = (new_qty * avg) / new_margin if new_margin else lev_use
                detail = f"BUY {label}: margine €{margin:,.2f} × {eff_lev:.1f}x → {qty:.6f} @ €{price:,.2f}."
                if eff_stop and lev_use < lev - 0.05:
                    detail += f" [leva {lev:.1f}x→{lev_use:.1f}x per stop a €{eff_stop:,.2f}]"

    elif action == "SELL":
        held = pos["qty"] if (pos and side == "long") else 0.0
        qty = held * size_pct / 100.0
        if held <= 1e-9 or qty * price < MIN_TRADE_EUR:
            detail, action = f"SELL {label} skipped — niente long da vendere.", "HOLD"
        else:
            entry = pos.get("avg_cost") or price
            margin = pos.get("collateral") or 0.0
            margin_release = margin * (qty / held)
            pnl = (price - entry) * qty
            cash += margin_release + pnl
            _set_cash(cash)
            new_qty = held - qty
            keep = new_qty > 1e-9
            _upsert_position(symbol, label, "long", new_qty, entry, margin - margin_release,
                             pos.get("active_stop") if keep else None,
                             pos.get("active_take") if keep else None, price)
            executed = True
            eur_amount = qty * price
            detail = f"SELL {label}: {qty:.6f} @ €{price:,.2f} (P&L €{pnl:,.2f})."

    elif action == "SHORT":
        if side == "long":
            detail, action = f"SHORT {label} ignorato — esiste una LONG aperta (prima SELL).", "HOLD"
        else:
            margin = cash * size_pct / 100.0   # margin locked
            if eur_cap is not None:
                margin = min(margin, eur_cap)
            margin = min(margin, cash)
            if margin < MIN_TRADE_EUR:
                detail, action = f"SHORT {label} skipped — €{margin:,.2f} di margine sotto minimo.", "HOLD"
            else:
                eff_stop = stop_loss if stop_loss is not None else (pos.get("active_stop") if pos else None)
                lev_use = _cap_leverage_to_stop(lev, price, eff_stop)
                notional = margin * lev_use
                qty = notional / price
                held = pos["qty"] if pos else 0.0
                new_qty = held + qty
                entry = (held * pos["avg_cost"] + qty * price) / new_qty if (pos and pos.get("avg_cost")) else price
                new_coll = (pos.get("collateral") or 0.0 if pos else 0.0) + margin
                cash -= margin
                _set_cash(cash)
                _upsert_position(symbol, label, "short", new_qty, entry, new_coll,
                                 stop_loss or (pos.get("active_stop") if pos else None),
                                 take_profit or (pos.get("active_take") if pos else None), price)
                executed = True
                eur_amount = margin
                eff_lev = (new_qty * entry) / new_coll if new_coll else lev_use
                detail = f"SHORT {label}: margine €{margin:,.2f} × {eff_lev:.1f}x → {qty:.6f} @ €{price:,.2f}."
                if eff_stop and lev_use < lev - 0.05:
                    detail += f" [leva {lev:.1f}x→{lev_use:.1f}x per stop a €{eff_stop:,.2f}]"

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


def _liquidation_price(p: dict[str, Any]) -> float | None:
    """
    Price at which unrealised loss wipes out the margin (collateral). Beyond it the
    position is force-closed and the margin is lost. None if unleveraged data missing.
      long  → entry - margin/qty   (entry × (1 - 1/leverage))
      short → entry + margin/qty   (entry × (1 + 1/leverage))
    """
    margin = p.get("collateral") or 0.0
    entry, qty = p.get("avg_cost"), p.get("qty") or 0.0
    if not entry or qty <= 1e-9 or margin <= 0:
        return None
    dist = margin / qty
    return entry - dist if p["side"] == "long" else entry + dist


def _effective_stop(p: dict[str, Any]) -> tuple[float | None, bool]:
    """
    Fold liquidation into the protective stop: price reaches the level nearer to
    spot first, so for a long that's the HIGHER of (stop, liq); for a short the
    LOWER. Returns (level, is_liquidation).
    """
    stop = p.get("active_stop")
    liq = _liquidation_price(p)
    if liq is None:
        return stop, False
    if p["side"] == "long":
        if stop is None or liq > stop:
            return liq, True
    else:
        if stop is None or liq < stop:
            return liq, True
    return stop, False


def intrabar_exit_decision(p: dict[str, Any], candles: list[tuple[int, float, float]]) -> dict[str, Any] | None:
    """
    Did the price touch this position's stop/target/liquidation BETWEEN cycles?
    Scans the (ts_ms, high, low) candles after the position's last update, in time
    order, and returns the level to fill at (as a resting order would) — or None.
    Stop/liquidation has priority over target within the same candle (conservative).
    """
    take, side = p.get("active_take"), p["side"]
    stop, stop_is_liq = _effective_stop(p)
    stop_lbl = "LIQUIDAZIONE" if stop_is_liq else "STOP-LOSS"
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
                return {"level": stop, "reason": f"{stop_lbl} {p['label']} (long) toccato a €{stop:,.2f} tra i cicli"}
            if take and hi >= take:
                return {"level": take, "reason": f"TAKE-PROFIT {p['label']} (long) toccato a €{take:,.2f} tra i cicli"}
        else:  # short
            if stop and hi >= stop:
                return {"level": stop, "reason": f"{stop_lbl} {p['label']} (short) toccato a €{stop:,.2f} tra i cicli"}
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
        take = p.get("active_take")
        stop, stop_is_liq = _effective_stop(p)
        stop_lbl = "LIQUIDAZIONE" if stop_is_liq else "STOP-LOSS"
        reason = close_action = fill = None
        if p["side"] == "long":
            if stop and price <= stop:
                reason, close_action, fill = f"{stop_lbl} {p['label']} (long) @ €{stop:,.2f}", "SELL", stop
            elif take and price >= take:
                reason, close_action, fill = f"TAKE-PROFIT {p['label']} (long) @ €{take:,.2f}", "SELL", take
        else:  # short
            if stop and price >= stop:
                reason, close_action, fill = f"{stop_lbl} {p['label']} (short) @ €{stop:,.2f}", "COVER", stop
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
            lev = p.get("leverage") or 1.0
            levs = f" {lev:.1f}x" if lev > 1.01 else ""
            liq = _liquidation_price(p) if lev > 1.01 else None
            liqs = f" liq €{liq:,.0f}" if liq else ""
            lines.append(
                f"   · {p['side'].upper()}{levs} {p['label']}: {p['qty']:.6f} @ €{(p['avg_cost'] or 0):,.2f} "
                f"(margin €{p['collateral']:,.2f}) → €{p['value']:,.2f} ({pnl}){stp}{tkp}{liqs}")
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
