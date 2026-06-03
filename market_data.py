"""
market_data.py — TradingView-independent market data.

Fetches OHLC history from CoinGecko and computes the same indicators the
chart-analysis skill uses (RSI, MACD, Bollinger Bands, EMA) directly in Python.

This lets the bot run on a headless host with NO TradingView Desktop and NO
local GUI — e.g. a cloud server that stays on while your PC is off.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests

OHLC_URL = "https://api.coingecko.com/api/v3/coins/{id}/ohlc"
MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"


def get_closes_eur(cg_id: str, days: int = 30) -> list[float]:
    """Closing prices (EUR) from CoinGecko OHLC candles. Newest last."""
    for attempt in range(3):
        try:
            r = requests.get(
                OHLC_URL.format(id=cg_id),
                params={"vs_currency": "eur", "days": days},
                timeout=15,
            )
            r.raise_for_status()
            return [float(c[4]) for c in r.json()]
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return []


def compute_indicators(closes: list[float]) -> dict[str, Any]:
    """RSI(14), MACD(12,26,9), Bollinger(20,2), EMA(50) from a close series."""
    if len(closes) < 30:
        return {}
    s = pd.Series(closes, dtype="float64")

    delta = s.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - 100 / (1 + rs)

    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal

    basis = s.rolling(20).mean()
    std = s.rolling(20).std()
    upper, lower = basis + 2 * std, basis - 2 * std
    ema50 = s.ewm(span=50, adjust=False).mean()

    def last(x):
        v = x.iloc[-1]
        return None if pd.isna(v) else float(v)

    out: dict[str, Any] = {}
    if last(rsi) is not None:
        out["RSI"] = round(last(rsi), 1)
    out["MACD"] = round(last(macd), 4)
    out["MACD_signal"] = round(last(signal), 4)
    out["MACD_hist"] = round(last(hist), 4)
    if last(basis) is not None:
        out["BB_upper"] = round(last(upper), 2)
        out["BB_lower"] = round(last(lower), 2)
    out["EMA50"] = round(last(ema50), 2)
    return out


def _indicator_studies(ind: dict[str, Any]) -> list[dict[str, Any]]:
    """Shape computed indicators like the TradingView `data values` output."""
    studies = []
    if "RSI" in ind:
        studies.append({"name": "Relative Strength Index", "values": {"RSI": ind["RSI"]}})
    if "MACD" in ind:
        studies.append({"name": "MACD", "values": {
            "MACD": ind["MACD"], "signal": ind["MACD_signal"], "hist": ind["MACD_hist"]}})
    if "BB_upper" in ind:
        studies.append({"name": "Bollinger Bands", "values": {
            "upper": ind["BB_upper"], "lower": ind["BB_lower"]}})
    if "EMA50" in ind:
        studies.append({"name": "Moving Average Exponential (50)", "values": {"EMA50": ind["EMA50"]}})
    return studies


def _market_chart_closes(cg_id: str, days: int) -> list[float]:
    """Close prices (EUR) from CoinGecko market_chart, granularity set by `days`.
    days=1 → ~5-min, days=2-90 → hourly, days=90+ → daily."""
    for attempt in range(3):
        try:
            r = requests.get(
                MARKET_CHART_URL.format(id=cg_id),
                params={"vs_currency": "eur", "days": days},
                timeout=15,
            )
            r.raise_for_status()
            return [float(p[1]) for p in r.json().get("prices", [])]
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return []


def _multi_timeframe_closes(cg_id: str) -> dict[str, list[float]]:
    """Close series at several horizons. Throttled to respect CoinGecko limits."""
    minute = _market_chart_closes(cg_id, 1)      # ~5-min candles
    time.sleep(1.5)
    hourly = _market_chart_closes(cg_id, 14)     # hourly candles
    time.sleep(1.5)
    daily = _market_chart_closes(cg_id, 365)     # daily candles
    weekly = daily[::7] if daily else []         # weekly ≈ every 7th daily
    return {"settimanale": weekly, "giornaliera": daily, "oraria": hourly, "minuti": minute}


def _trend_read(closes: list[float]) -> dict[str, Any] | None:
    """Compact trend read for one timeframe: direction + RSI + MACD sign."""
    ind = compute_indicators(closes)
    if not ind or not closes:
        return None
    price, ema, hist = closes[-1], ind.get("EMA50"), ind.get("MACD_hist", 0.0)
    if ema is None:
        trend = "n/d"
    elif price > ema and hist > 0:
        trend = "rialzista"
    elif price < ema and hist < 0:
        trend = "ribassista"
    else:
        trend = "laterale"
    return {"trend": trend, "rsi": ind.get("RSI"), "macd": "+" if hist >= 0 else "-"}


def multi_timeframe_reads(closes_by_tf: dict[str, list[float]]) -> dict[str, Any]:
    return {tf: _trend_read(c) for tf, c in closes_by_tf.items()}


def multi_timeframe(cg_id: str) -> dict[str, Any]:
    """Public: per-timeframe trend reads for one coin."""
    return multi_timeframe_reads(_multi_timeframe_closes(cg_id))


def build_snapshot(entry: dict[str, str], price: float) -> dict[str, Any]:
    """
    Full snapshot for one universe entry (CoinGecko only): price, multi-timeframe
    trend reads, plus base indicators + OHLCV-style summary derived from the
    hourly series (the primary working timeframe).
    """
    tf_closes = _multi_timeframe_closes(entry["cg"])
    mtf = multi_timeframe_reads(tf_closes)
    base = tf_closes.get("oraria") or tf_closes.get("giornaliera") or []
    ind = compute_indicators(base)

    ohlcv = None
    if base:
        window = base[-60:]
        first, lastc = window[0], window[-1]
        ohlcv = {
            "bar_count": len(window), "open": first, "close": lastc,
            "high": max(window), "low": min(window), "range": max(window) - min(window),
            "change_pct": f"{(lastc - first) / first * 100:+.2f}%" if first else "—",
            "avg_volume": "—", "last_5_bars": [{"close": c} for c in window[-5:]],
        }
    return {
        "source": "coingecko", "symbol": entry["tv"], "label": entry["label"],
        "price": price, "ohlcv": ohlcv, "indicators": _indicator_studies(ind), "mtf": mtf,
    }
