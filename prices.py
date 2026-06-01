"""
prices.py — Live market data.

Stocks / ETFs via yfinance, crypto via the CoinGecko public API (no key needed).

A "holding" is a dict:
    {
        "symbol": "AAPL",          # display symbol
        "type": "stock",            # "stock" | "crypto"
        "quantity": 10.0,
        "coingecko_id": None,       # required when type == "crypto" (e.g. "bitcoin")
        "cost_basis": 150.0          # optional, per-unit average cost
    }
"""

from __future__ import annotations

import time
from typing import Any

import requests

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
_CACHE_TTL = 60  # seconds
_cache: dict[str, tuple[float, float]] = {}  # key -> (price, fetched_at)


def _cached(key: str) -> float | None:
    hit = _cache.get(key)
    if hit and (time.time() - hit[1]) < _CACHE_TTL:
        return hit[0]
    return None


def _store(key: str, price: float) -> float:
    _cache[key] = (price, time.time())
    return price


def get_stock_price(symbol: str) -> float | None:
    """Latest price for a stock/ETF symbol, or None on failure."""
    key = f"stock:{symbol}"
    if (c := _cached(key)) is not None:
        return c
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(symbol)
        price = None
        # fast_info is the cheapest path
        try:
            price = float(ticker.fast_info["last_price"])
        except Exception:
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        if price and price > 0:
            return _store(key, price)
    except Exception:
        return None
    return None


def get_crypto_prices(coingecko_ids: list[str]) -> dict[str, float]:
    """Batch crypto prices in USD keyed by CoinGecko id."""
    ids = [c for c in coingecko_ids if c]
    if not ids:
        return {}
    try:
        resp = requests.get(
            COINGECKO_URL,
            params={"ids": ",".join(sorted(set(ids))), "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        out: dict[str, float] = {}
        for cid, payload in data.items():
            usd = payload.get("usd")
            if usd is not None:
                out[cid] = _store(f"crypto:{cid}", float(usd))
        return out
    except Exception:
        return {}


def price_holdings(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Resolve live prices for every holding and compute valuation.

    Returns:
        {
          "positions": [ {symbol, type, quantity, price, value,
                          cost_basis, pnl, pnl_pct}, ... ],
          "total_value": float,
          "total_cost": float,
          "total_pnl": float,
          "errors": [symbol, ...]
        }
    """
    crypto_ids = [h["coingecko_id"] for h in holdings if h.get("type") == "crypto"]
    crypto_prices = get_crypto_prices(crypto_ids)

    positions: list[dict[str, Any]] = []
    errors: list[str] = []
    total_value = total_cost = 0.0

    for h in holdings:
        symbol = h.get("symbol", "?")
        qty = float(h.get("quantity") or 0)
        cost_basis = h.get("cost_basis")

        if h.get("type") == "crypto":
            price = crypto_prices.get(h.get("coingecko_id"))
        else:
            price = get_stock_price(symbol)

        if price is None:
            errors.append(symbol)
            price = 0.0

        value = price * qty
        total_value += value
        pnl = pnl_pct = None
        if cost_basis:
            cost = float(cost_basis) * qty
            total_cost += cost
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost else None

        positions.append(
            {
                "symbol": symbol,
                "type": h.get("type", "stock"),
                "quantity": qty,
                "price": price,
                "value": value,
                "cost_basis": cost_basis,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )

    return {
        "positions": positions,
        "total_value": total_value,
        "total_cost": total_cost,
        "total_pnl": (total_value - total_cost) if total_cost else None,
        "errors": errors,
    }


def format_prices_for_prompt(valuation: dict[str, Any]) -> str:
    """Compact, model-friendly rendering of the current portfolio valuation."""
    if not valuation["positions"]:
        return "Portfolio is empty — no holdings configured."

    lines = ["| Symbol | Type | Qty | Price (USD) | Value (USD) | P&L % |",
             "|--------|------|-----|-------------|-------------|-------|"]
    for p in valuation["positions"]:
        pnl_pct = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "—"
        lines.append(
            f"| {p['symbol']} | {p['type']} | {p['quantity']:g} | "
            f"{p['price']:,.2f} | {p['value']:,.2f} | {pnl_pct} |"
        )
    lines.append("")
    lines.append(f"**Total value: ${valuation['total_value']:,.2f}**")
    if valuation["total_pnl"] is not None:
        lines.append(f"**Total P&L: ${valuation['total_pnl']:,.2f}**")
    if valuation["errors"]:
        lines.append(f"_Price unavailable for: {', '.join(valuation['errors'])}_")
    return "\n".join(lines)
