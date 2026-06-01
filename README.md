# AI Investment Advisor + Crypto Paper-Trading Bot

A personal, AI-driven investment dashboard and an autonomous **paper-trading**
bot for crypto, powered by Claude. It analyses markets through your own written
investment philosophy and a focused set of technical indicators.

> ⚠️ **Simulated money only.** The bot trades a virtual account (starts at
> €1000). It never places real orders, never touches an exchange, and never
> moves real funds. Nothing here is financial advice.

## What it does

- **Dashboard** (`app.py`, Streamlit, dark theme): morning brief, market scan,
  chat with the strategist, the bot tab, and a portfolio book view.
- **Crypto paper bot** (`btc_bot.py`): each cycle it reads a focused universe
  (BTC, ETH, SOL, XRP, ADA), reads RSI / MACD / Bollinger / EMA, and asks Claude
  for a set of orders — guided by `philosophy.md`. It keeps a focused book
  (max 4 positions), stays fully invested, and enforces automatic stop-loss /
  take-profit per position.
- **Two data sources**: your live **TradingView Desktop** chart (via the bundled
  CDP bridge) when available; otherwise **CoinGecko** with indicators computed in
  Python — so it runs headless with no TradingView and no GUI.
- **24/7 in the cloud**: a GitHub Actions workflow runs the bot on a schedule, so
  it keeps trading while your computer is off. See `BOT_DEPLOY.md`.

## Run locally

```bash
pip install -r requirements.txt
# set env vars (or use a .env — see .env.example)
#   ANTHROPIC_API_KEY  (required)
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (optional)
streamlit run app.py
```

Run a single bot cycle from the terminal:

```bash
python btc_bot.py --once            # one cycle
python btc_bot.py --loop 1800       # every 30 min
python btc_bot.py --once --telegram # also notify Telegram
python btc_bot.py --reset           # reset the virtual account to €1000
```

## Run 24/7 in the cloud

See **`BOT_DEPLOY.md`** — push to a **private** GitHub repo, add the three
secrets, enable Actions. The workflow runs every 30 minutes and commits the
virtual account state (`advisor.db`) back to the repo.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Streamlit dashboard (dark theme) |
| `analyzer.py` | Claude wrapper — morning brief, market scan, chat, Telegram update |
| `btc_bot.py` | Multi-crypto paper-trading bot (decisions + execution) |
| `paper_broker.py` | Virtual multi-asset account (SQLite) |
| `btc_feed.py` | Market data: TradingView (CDP) + CoinGecko fallback |
| `market_data.py` | CoinGecko OHLC + indicators computed in Python |
| `prompts.py` | System prompts; injects philosophy + skills |
| `skills.py` | Loads the analysis skills in `skills/` |
| `prices.py` | Stock/ETF (yfinance) + crypto prices for the advisor |
| `memory.py` | SQLite: conversation history, holdings, snapshots |
| `telegram_alert.py` | Telegram sender |
| `philosophy.md` | Your personal investment philosophy (drives every analysis) |

## Configuration

| Env var | Required | Notes |
|---------|----------|-------|
| `ANTHROPIC_API_KEY` | yes | Claude API key |
| `TELEGRAM_BOT_TOKEN` | no | For Telegram notifications |
| `TELEGRAM_CHAT_ID` | no | Your Telegram chat id |
| `ADVISOR_DATA_SOURCE` | no | Set to `coingecko` on headless hosts (no TradingView) |

Built on top of the bundled TradingView CDP bridge (Node, in `src/`). Not
affiliated with TradingView Inc. or Anthropic.
