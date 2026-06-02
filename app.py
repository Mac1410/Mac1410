"""
app.py — Streamlit dashboard (dark, high-end theme).

Run:
    streamlit run app.py

Environment:
    ANTHROPIC_API_KEY     (required)
    TELEGRAM_BOT_TOKEN    (optional — for the morning Telegram push)
    TELEGRAM_CHAT_ID      (optional)
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st

import analyzer
import btc_bot
import btc_feed
import memory
import paper_broker
from prices import format_prices_for_prompt, price_holdings
from telegram_alert import is_configured as telegram_configured

st.set_page_config(
    page_title="Investment Advisor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

memory.init_db()

# --------------------------------------------------------------------------- CSS
st.markdown(
    """
    <style>
      .stApp { background: radial-gradient(1200px 600px at 20% -10%, #14181f 0%, #0B0E11 60%); }
      .block-container { padding-top: 2rem; max-width: 1300px; }
      h1, h2, h3 { letter-spacing: .3px; }
      .hero-title {
        font-size: 2.1rem; font-weight: 800;
        background: linear-gradient(90deg, #F5E6A8 0%, #D4AF37 50%, #B8902A 100%);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        margin-bottom: .1rem;
      }
      .hero-sub { color: #8A929B; font-size: .95rem; margin-bottom: 1.2rem; }
      div[data-testid="stMetric"] {
        background: linear-gradient(180deg, #181D24 0%, #11151B 100%);
        border: 1px solid #232A33; border-radius: 14px; padding: 16px 18px;
        box-shadow: 0 8px 24px rgba(0,0,0,.35);
      }
      div[data-testid="stMetricValue"] { font-weight: 700; }
      .stTabs [data-baseweb="tab"] { font-weight: 600; }
      .stButton>button {
        border-radius: 10px; border: 1px solid #2A323C; font-weight: 600;
      }
      .stButton>button:hover { border-color: #D4AF37; color: #D4AF37; }
      .disclaimer { color: #6B7280; font-size: .8rem; border-top: 1px solid #232A33;
                    margin-top: 2rem; padding-top: .8rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="hero-title">Investment Advisor</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-sub">McKinsey-grade analysis on your book · powered by Claude · '
    "analysis only, never executes trades</div>",
    unsafe_allow_html=True,
)

if not os.environ.get("ANTHROPIC_API_KEY"):
    st.error("`ANTHROPIC_API_KEY` is not set. Add it to your environment and reload.")

# --------------------------------------------------------------------- SIDEBAR
with st.sidebar:
    st.header("📊 Portfolio")
    holdings = memory.get_holdings()

    if holdings:
        st.caption(f"{len(holdings)} holdings")
        for h in holdings:
            cols = st.columns([3, 1])
            label = f"**{h['symbol']}** · {h['quantity']:g} ({h['type']})"
            cols[0].markdown(label)
            if cols[1].button("✕", key=f"del_{h['symbol']}", help="Remove"):
                memory.delete_holding(h["symbol"])
                st.rerun()
    else:
        st.caption("No holdings yet — add one below.")

    with st.form("add_holding", clear_on_submit=True):
        st.subheader("Add / update holding")
        symbol = st.text_input("Symbol", placeholder="AAPL or BTC")
        type_ = st.selectbox("Type", ["stock", "crypto"])
        quantity = st.number_input("Quantity", min_value=0.0, step=1.0, format="%.6f")
        coingecko_id = st.text_input(
            "CoinGecko id (crypto only)", placeholder="bitcoin",
            help="Required for crypto — e.g. bitcoin, ethereum, solana",
        )
        cost_basis = st.number_input(
            "Avg cost / unit (optional)", min_value=0.0, step=1.0, format="%.4f"
        )
        if st.form_submit_button("Save holding", use_container_width=True):
            if symbol and quantity > 0:
                memory.upsert_holding(
                    symbol=symbol,
                    type_=type_,
                    quantity=quantity,
                    coingecko_id=(coingecko_id or None) if type_ == "crypto" else None,
                    cost_basis=cost_basis or None,
                )
                st.rerun()
            else:
                st.warning("Symbol and a positive quantity are required.")

    st.divider()
    st.subheader("📨 Telegram")
    if telegram_configured():
        if st.button("Send morning update", use_container_width=True):
            with st.spinner("Generating and sending…"):
                ok, detail = analyzer.telegram_morning_update()
            (st.success if ok else st.error)(detail)
    else:
        st.caption("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable.")

# ------------------------------------------------------------------ VALUATION
valuation = price_holdings(memory.get_holdings())

m1, m2, m3 = st.columns(3)
m1.metric("Portfolio value", f"${valuation['total_value']:,.0f}")
if valuation["total_pnl"] is not None:
    pnl = valuation["total_pnl"]
    pct = (pnl / valuation["total_cost"] * 100) if valuation["total_cost"] else 0
    m2.metric("Unrealized P&L", f"${pnl:,.0f}", f"{pct:+.1f}%")
else:
    m2.metric("Unrealized P&L", "—", help="Add cost basis to holdings")
m3.metric("Positions", str(len(valuation["positions"])))

# --------------------------------------------------------------------- TABS
tab_brief, tab_scan, tab_chat, tab_bot, tab_book = st.tabs(
    ["🌅 Morning Brief", "🔭 Market Scan", "💬 Chat", "🤖 BTC Bot", "📒 Book"]
)

with tab_brief:
    st.subheader("Morning Brief")
    if st.button("Generate morning brief", type="primary"):
        with st.spinner("Thinking through your book…"):
            st.session_state["brief"] = analyzer.morning_brief()
    if st.session_state.get("brief"):
        st.markdown(st.session_state["brief"])

with tab_scan:
    st.subheader("Market Scan")
    if st.button("Run market scan", type="primary"):
        with st.spinner("Scanning the regime and your positions…"):
            st.session_state["scan"] = analyzer.market_scan()
    if st.session_state.get("scan"):
        st.markdown(st.session_state["scan"])

with tab_chat:
    st.subheader("Ask the strategist")
    col_a, col_b = st.columns([5, 1])
    if col_b.button("Clear", use_container_width=True):
        memory.clear_conversation()
        st.rerun()

    for msg in memory.get_recent_messages(limit=40):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("e.g. Is my crypto allocation too aggressive?"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            st.write_stream(analyzer.chat(prompt))
        st.rerun()

with tab_book:
    st.subheader("Holdings & valuation")
    if valuation["positions"]:
        df = pd.DataFrame(valuation["positions"])
        df = df.rename(
            columns={
                "symbol": "Symbol", "type": "Type", "quantity": "Qty",
                "price": "Price", "value": "Value", "cost_basis": "Cost/unit",
                "pnl": "P&L", "pnl_pct": "P&L %",
            }
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.markdown("##### Value over time")
        snaps = memory.get_snapshots(limit=90)
        if snaps:
            hist = pd.DataFrame(snaps)
            hist["ts"] = pd.to_datetime(hist["ts"])
            st.line_chart(hist.set_index("ts")["total_value"])
        if st.button("📸 Save snapshot"):
            memory.save_snapshot(valuation)
            st.success("Snapshot saved.")
            st.rerun()
    else:
        st.info("Add holdings from the sidebar to see your book.")

    with st.expander("What the model sees (live context)"):
        st.markdown(format_prices_for_prompt(valuation))

with tab_bot:
    st.subheader("Crypto paper-trading bot")
    st.caption(
        "Virtual money only (€1000 start). Actively trades a focused universe "
        "(BTC, ETH, SOL, XRP, ADA) on your live TradingView charts — max 4 "
        "positions, auto stop-loss / take-profit. It never sends real orders."
    )

    paper_broker.init_paper()

    @st.cache_data(ttl=60)
    def _live_prices():
        try:
            return btc_feed.live_prices()
        except Exception:
            return {}

    bot_state = paper_broker.get_state(_live_prices())

    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Account value", f"€{bot_state['value']:,.2f}")
    b2.metric("Cash", f"€{bot_state['cash_eur']:,.2f}")
    b3.metric("Positions", f"{bot_state['n_positions']}/{btc_bot.MAX_POSITIONS}")
    b4.metric("P&L", f"€{bot_state['pnl']:,.2f}", f"{bot_state['pnl_pct']:+.2f}%")

    c1, c2, c3 = st.columns([2, 2, 1])
    notify = c2.checkbox("Send result to Telegram", value=False,
                         disabled=not telegram_configured())
    if c1.button("▶ Run bot cycle", type="primary"):
        with st.spinner("Reading the universe on TradingView and deciding…"):
            st.session_state["bot_out"] = btc_bot.run_cycle(notify_telegram=notify)
        st.rerun()
    if c3.button("Reset €1000"):
        paper_broker.reset()
        st.session_state.pop("bot_out", None)
        st.rerun()

    out = st.session_state.get("bot_out")
    if out and out.get("ok"):
        for ex in out.get("auto_exits", []):
            st.warning(f"🔔 {ex['exit_reason']} — {ex['detail']}")
        st.markdown(f"**Rationale:** {out['decision'].get('rationale', '')}")
        orders = out["decision"].get("orders", [])
        if orders:
            st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
        for r in out.get("results", []):
            (st.success if r.get("executed") else st.caption)(r["detail"])
        if out.get("snaps"):
            st.caption(f"Data source: {next(iter(out['snaps'].values()))['source']}")
    elif out:
        st.warning(out.get("detail", "No data."))

    if bot_state["positions"]:
        st.markdown("##### Open positions")
        pdf = pd.DataFrame(bot_state["positions"])[
            ["side", "label", "qty", "avg_cost", "price", "value", "pnl_pct", "active_stop", "active_take"]
        ].rename(columns={
            "side": "Side", "label": "Asset", "qty": "Qty", "avg_cost": "Entry", "price": "Price",
            "value": "Value", "pnl_pct": "P&L %", "active_stop": "Stop", "active_take": "Target",
        })
        pdf["Side"] = pdf["Side"].str.upper()
        st.dataframe(pdf, use_container_width=True, hide_index=True)

    st.markdown("##### Andamento vs solo-BTC (buy & hold)")
    hist_rows = paper_broker.get_bot_snapshots()
    bench = paper_broker.get_benchmark()
    if hist_rows and bench.get("btc_qty"):
        hist = pd.DataFrame(hist_rows)
        hist["ts"] = pd.to_datetime(hist["ts"])
        hist["Bot"] = hist["account_value"]
        hist["Solo BTC"] = hist["btc_price"] * bench["btc_qty"]
        last = hist.iloc[-1]
        diff = last["Bot"] - last["Solo BTC"]
        diff_pct = (diff / last["Solo BTC"] * 100) if last["Solo BTC"] else 0
        d1, d2, d3 = st.columns(3)
        d1.metric("Bot", f"€{last['Bot']:,.2f}")
        d2.metric("Solo BTC (hold)", f"€{last['Solo BTC']:,.2f}")
        d3.metric("Bot vs BTC", f"€{diff:,.2f}", f"{diff_pct:+.2f}%")
        st.line_chart(hist.set_index("ts")[["Bot", "Solo BTC"]])
        st.caption("«Solo BTC» = se i €1000 iniziali fossero stati messi tutti in "
                   "Bitcoin e tenuti. I punti si aggiungono a ogni ciclo del bot.")
    else:
        st.caption("Confronto vs BTC: in attesa di dati — si popola a ogni ciclo del bot. "
                   "(In locale fai `git pull` per vedere i cicli eseguiti nel cloud.)")

    st.markdown("##### Trade log")
    trades = [t for t in paper_broker.get_trades(limit=80) if t["action"] != "HOLD"]
    if trades:
        tdf = pd.DataFrame(trades)[
            ["ts", "label", "action", "price", "qty", "eur_amount", "cash_after", "confidence", "thesis"]
        ].rename(columns={
            "ts": "Time", "label": "Asset", "action": "Action", "price": "Price",
            "qty": "Qty", "eur_amount": "EUR", "cash_after": "Cash", "confidence": "Conf",
            "thesis": "Thesis",
        })
        st.dataframe(tdf, use_container_width=True, hide_index=True)
    else:
        st.caption("No trades yet — run a cycle to start.")

st.markdown(
    '<div class="disclaimer">This tool provides AI-generated analysis for '
    "informational purposes only — it is not financial advice and does not place "
    "trades or move funds. Always do your own research and size positions to your "
    "own risk tolerance.</div>",
    unsafe_allow_html=True,
)
