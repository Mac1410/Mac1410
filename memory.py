"""
memory.py — SQLite persistence for conversation history, portfolio holdings,
and portfolio snapshots.

Single-file DB so the dashboard keeps state across restarts. No external deps.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.environ.get("ADVISOR_DB_PATH", os.path.join(os.path.dirname(__file__), "advisor.db"))


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                role      TEXT NOT NULL,           -- 'user' | 'assistant'
                content   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS holdings (
                symbol        TEXT PRIMARY KEY,
                type          TEXT NOT NULL,        -- 'stock' | 'crypto'
                quantity      REAL NOT NULL,
                coingecko_id  TEXT,
                cost_basis    REAL
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                total_value   REAL,
                total_pnl     REAL,
                positions     TEXT                  -- JSON blob
            );
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- conversations

def add_message(role: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversations (ts, role, content) VALUES (?, ?, ?)",
            (_now(), role, content),
        )


def get_recent_messages(limit: int = 20) -> list[dict[str, str]]:
    """Return the last `limit` messages in chronological order."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_conversation() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM conversations")


# -------------------------------------------------------------------- holdings

def get_holdings() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


def upsert_holding(
    symbol: str,
    type_: str,
    quantity: float,
    coingecko_id: str | None = None,
    cost_basis: float | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO holdings (symbol, type, quantity, coingecko_id, cost_basis)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                type=excluded.type,
                quantity=excluded.quantity,
                coingecko_id=excluded.coingecko_id,
                cost_basis=excluded.cost_basis
            """,
            (symbol.upper().strip(), type_, quantity, coingecko_id, cost_basis),
        )


def delete_holding(symbol: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM holdings WHERE symbol = ?", (symbol.upper().strip(),))


# ----------------------------------------------------------------- snapshots

def save_snapshot(valuation: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_snapshots (ts, total_value, total_pnl, positions)
            VALUES (?, ?, ?, ?)
            """,
            (
                _now(),
                valuation.get("total_value"),
                valuation.get("total_pnl"),
                json.dumps(valuation.get("positions", [])),
            ),
        )


def get_snapshots(limit: int = 90) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ts, total_value, total_pnl FROM portfolio_snapshots "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
