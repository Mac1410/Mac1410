"""
btc_feed.py — Live BTC market data.

Primary source: the running TradingView Desktop chart, read via the repo's
Node CLI over CDP (port 9222). Fallback: CoinGecko (BTC/EUR) if TradingView
is closed or CDP is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

import requests

import market_data

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(REPO_DIR, "src", "cli", "index.js")
DEFAULT_SYMBOL = "BINANCE:BTCEUR"

# Indicators the chart-analysis skill wants for a momentum/trend/volatility read.
DESIRED_INDICATORS = [
    "Relative Strength Index",
    "MACD",
    "Bollinger Bands",
    "Moving Average Exponential",
]

# Focused crypto universe (philosophy: conviction, not over-diversification).
CRYPTO_UNIVERSE = [
    {"tv": "BINANCE:BTCEUR", "label": "BTC", "cg": "bitcoin"},
    {"tv": "BINANCE:ETHEUR", "label": "ETH", "cg": "ethereum"},
    {"tv": "BINANCE:SOLEUR", "label": "SOL", "cg": "solana"},
    {"tv": "BINANCE:XRPEUR", "label": "XRP", "cg": "ripple"},
    {"tv": "BINANCE:ADAEUR", "label": "ADA", "cg": "cardano"},
]


def _node_exe() -> str:
    exe = shutil.which("node")
    if exe:
        return exe
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ):
        if os.path.exists(cand):
            return cand
    return "node"


def _run_cli(args: list[str], timeout: int = 30) -> tuple[int, dict[str, Any]]:
    try:
        proc = subprocess.run(
            [_node_exe(), CLI_PATH, *args],
            capture_output=True, text=True, cwd=REPO_DIR, timeout=timeout,
        )
    except Exception:
        return 1, {}
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {}
    return proc.returncode, data


def _cg_headers() -> dict[str, str]:
    key = os.environ.get("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def _coingecko_btc_eur() -> float | None:
    return _coingecko_multi(["bitcoin"]).get("bitcoin")


def _coingecko_multi(cg_ids: list[str]) -> dict[str, float]:
    ids = [c for c in cg_ids if c]
    if not ids:
        return {}
    for attempt in range(3):
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ",".join(sorted(set(ids))), "vs_currencies": "eur"},
                headers=_cg_headers(),
                timeout=12,
            )
            r.raise_for_status()
            return {cid: float(v["eur"]) for cid, v in r.json().items() if "eur" in v}
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # 2s, 4s backoff
    return {}


def ensure_indicators() -> None:
    """
    Per the chart-analysis skill: make sure the momentum/trend/volatility studies
    are on the chart. Only adds the ones that are missing (avoids duplicates).
    """
    _, state = _run_cli(["state"])
    existing = {s.get("name", "") for s in state.get("studies", [])}
    added = False
    for name in DESIRED_INDICATORS:
        if name not in existing:
            rc, res = _run_cli(["indicator", "add", name])
            if res.get("success"):
                added = True
    if added:
        time.sleep(3)  # let the new studies compute before reading values


def get_study_values() -> list[dict[str, Any]]:
    """Current numeric readings from visible indicators (data_get_study_values)."""
    _, res = _run_cli(["values"])
    return res.get("studies", []) if res.get("success") else []


def get_btc_snapshot(symbol: str = DEFAULT_SYMBOL) -> dict[str, Any]:
    """
    Returns:
        {
          "source": "tradingview" | "coingecko" | "none",
          "symbol": str,
          "price": float | None,
          "quote": dict | None,
          "ohlcv": dict | None,   # summary stats
        }
    """
    # --- Try TradingView via CDP ---
    _run_cli(["symbol", symbol])
    quote = None
    for _ in range(5):
        rc, q = _run_cli(["quote"])
        if q.get("success") and q.get("last"):
            quote = q
            break
        time.sleep(2)

    if quote and quote.get("last"):
        _, ohlcv = _run_cli(["ohlcv", "-n", "60", "-s"])
        ensure_indicators()
        indicators = get_study_values()
        _, state = _run_cli(["state"])
        return {
            "source": "tradingview",
            "symbol": quote.get("symbol", symbol),
            "resolution": state.get("resolution"),
            "price": float(quote["last"]),
            "quote": quote,
            "ohlcv": ohlcv if ohlcv.get("success") else None,
            "indicators": indicators,
        }

    # --- Fallback: CoinGecko ---
    price = _coingecko_btc_eur()
    if price:
        return {
            "source": "coingecko",
            "symbol": "BTC/EUR",
            "price": price,
            "quote": None,
            "ohlcv": None,
        }

    return {"source": "none", "symbol": symbol, "price": None, "quote": None, "ohlcv": None}


def format_snapshot(snap: dict[str, Any]) -> str:
    """Model-friendly markdown summary of the BTC snapshot."""
    if snap["price"] is None:
        return "⚠️ No BTC price available (TradingView closed and CoinGecko unreachable)."

    src = {
        "tradingview": "TradingView (live chart, BINANCE:BTCEUR)",
        "coingecko": "CoinGecko (TradingView unavailable)",
    }.get(snap["source"], snap["source"])

    lines = [
        f"Data source: {src}",
        f"BTC price: €{snap['price']:,.2f}",
    ]
    if snap.get("resolution"):
        lines.append(f"Chart timeframe: {snap['resolution']}")

    inds = snap.get("indicators") or []
    if inds:
        lines.append("")
        lines.append("Live indicators (from the chart):")
        for study in inds:
            vals = ", ".join(f"{k} {v}" for k, v in (study.get("values") or {}).items())
            lines.append(f"- {study.get('name')}: {vals}")

    o = snap.get("ohlcv")
    if o:
        lines += [
            "",
            f"Last {o.get('bar_count', '?')} bars:",
            f"- Open €{o.get('open', 0):,.2f} → Close €{o.get('close', 0):,.2f}",
            f"- High €{o.get('high', 0):,.2f} / Low €{o.get('low', 0):,.2f}",
            f"- Range €{o.get('range', 0):,.2f} | Change {o.get('change_pct', '—')}",
            f"- Avg volume {o.get('avg_volume', '—')}",
        ]
        bars = o.get("last_5_bars") or []
        if bars:
            lines.append("- Recent closes: " + ", ".join(f"€{b['close']:,.0f}" for b in bars))
    return "\n".join(lines)


# ---------------------------------------------------------------- universe

def _fetch_one(tv: str, label: str) -> dict[str, Any] | None:
    """Fetch one symbol's quote + OHLCV + indicator values from TradingView."""
    _run_cli(["symbol", tv])
    quote = None
    for _ in range(3):
        _, q = _run_cli(["quote"])
        if q.get("success") and q.get("last") and q.get("symbol") == tv:
            quote = q
            break
        time.sleep(1.5)
    if not (quote and quote.get("last")):
        return None
    _, ohlcv = _run_cli(["ohlcv", "-n", "60", "-s"])
    return {
        "source": "tradingview", "symbol": tv, "label": label,
        "price": float(quote["last"]),
        "ohlcv": ohlcv if ohlcv.get("success") else None,
        "indicators": get_study_values(),
    }


def get_universe_snapshot(universe: list[dict[str, str]] | None = None) -> dict[str, dict[str, Any]]:
    """
    Fetch a snapshot for each symbol in the universe. Tries TradingView first
    (adding the skill indicators once), and falls back to CoinGecko prices for
    the whole universe if the chart/CDP is unavailable.

    Returns: { tv_symbol: snapshot }
    """
    universe = universe or CRYPTO_UNIVERSE

    # Force CoinGecko mode on headless hosts (no TradingView Desktop / GUI).
    force_cg = os.environ.get("ADVISOR_DATA_SOURCE", "").lower() == "coingecko"

    if not force_cg:
        ensure_indicators()  # add studies once; they recompute as we switch symbols
        snaps: dict[str, dict[str, Any]] = {}
        tv_ok = True
        for entry in universe:
            snap = _fetch_one(entry["tv"], entry["label"])
            if snap is None:
                tv_ok = False
                break
            analysis = market_data.multi_timeframe(entry["cg"], snap["price"])  # MTF + levels from CoinGecko
            snap["mtf"] = analysis["reads"]
            snap["levels"] = analysis["levels"]
            snaps[entry["tv"]] = snap
            time.sleep(1.0)  # throttle CoinGecko (multi-timeframe is call-heavy)
        if tv_ok and snaps:
            return snaps

    # CoinGecko path: live prices + indicators COMPUTED in Python (no TradingView).
    prices = _coingecko_multi([e["cg"] for e in universe])
    snaps = {}
    for e in universe:
        px = prices.get(e["cg"])
        if px:
            snaps[e["tv"]] = market_data.build_snapshot(e, px)
            time.sleep(1.0)  # throttle CoinGecko (multi-timeframe is call-heavy)
    return snaps


def prices_from_snapshots(snaps: dict[str, dict[str, Any]]) -> dict[str, float]:
    return {sym: s["price"] for sym, s in snaps.items() if s.get("price")}


def live_prices(universe: list[dict[str, str]] | None = None) -> dict[str, float]:
    """Fast live prices {tv_symbol: eur} via one CoinGecko call (no indicators)."""
    universe = universe or CRYPTO_UNIVERSE
    cg = _coingecko_multi([e["cg"] for e in universe])
    return {e["tv"]: cg[e["cg"]] for e in universe if e["cg"] in cg}


def format_universe(snaps: dict[str, dict[str, Any]]) -> str:
    """Compact multi-symbol briefing for the model."""
    if not snaps:
        return "⚠️ No market data available for the crypto universe."
    src = next(iter(snaps.values())).get("source", "?")
    src_label = {"tradingview": "TradingView live charts", "coingecko": "CoinGecko (TradingView unavailable)"}.get(src, src)
    out = [f"Data source: {src_label}", ""]
    for sym, s in snaps.items():
        o = s.get("ohlcv") or {}
        chg = o.get("change_pct", "—")
        out.append(f"### {s['label']}  (€{s['price']:,.2f}, 60-bar change {chg})")
        inds = s.get("indicators") or []
        if inds:
            for study in inds:
                vals = ", ".join(f"{k} {v}" for k, v in (study.get("values") or {}).items())
                out.append(f"- {study.get('name')}: {vals}")
        if o:
            out.append(f"- range €{o.get('low', 0):,.0f}–€{o.get('high', 0):,.0f}, avg vol {o.get('avg_volume', '—')}")
        mtf = s.get("mtf")
        if mtf:
            out.append("- Multi-timeframe (trend / RSI / MACD):")
            for tf_name in ("mensile", "settimanale", "giornaliera", "oraria", "minuti"):
                r = mtf.get(tf_name)
                if r:
                    out.append(f"   · {tf_name}: {r['trend']} / RSI {r.get('rsi')} / MACD {r.get('macd')}")
        lv = s.get("levels")
        if lv:
            rows = []
            for tf_name in ("settimanale", "giornaliera", "oraria"):
                k = lv.get(tf_name)
                if k and (k.get("support") or k.get("resistance")):
                    rows.append(f"   · {tf_name}: supporto €{k.get('support')} / resistenza €{k.get('resistance')}")
            if rows:
                out.append("- Massimi/minimi locali (supporto sotto / resistenza sopra il prezzo):")
                out += rows
        out.append("")
    return "\n".join(out)
