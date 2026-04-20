"""
GME Trading System Dashboard
Run: streamlit run dashboard.py
"""
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=True)   # always pick up latest .env values

ET = ZoneInfo("America/New_York")

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

st.set_page_config(page_title="GME Agent System", layout="wide", page_icon="🎮")

# ── Console helpers ───────────────────────────────────────────────────────────

AGENT_ROSTER = [
    ("Synthesis", "every 5 min",    "Cross-Agent Brief"),
    ("Valerie",   "every 1 min",    "Data Validator"),
    ("Chatty",    "every 30 sec",   "Stream Commentator"),
    ("Newsie",    "every 30 min",   "News Sentiment"),
    ("Pattern",   "every 2 hrs",    "Chart Patterns"),
    ("Trendy",    "every 4 hrs",    "Daily Trend"),
    ("Futurist",  "every 2 hrs",    "Price Predictor"),
    ("Boss",      "event-driven",   "Risk Approval"),
    ("CTO",       "9:05 AM / Sun",  "Structural Intel"),
    ("Briefing",  "9:32 AM ET",     "Daily Brief"),
    ("Social",    "every 15 min",   "Social Monitor"),
]

_IDLE_THRESHOLD_S  = 300    # >5 min since last log → idle
_ACTIVE_THRESHOLD_S = 120   # <2 min since last log → active


def _agent_card_data() -> list[dict]:
    now = datetime.now(ET).replace(tzinfo=None)
    cards = []
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    for name, schedule, role in AGENT_ROSTER:
        try:
            row = conn.execute(
                "SELECT timestamp, task_type, status, content FROM agent_logs "
                "WHERE agent_name=? ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        except Exception:
            row = None

        if row is None:
            cards.append({"name": name, "role": role, "schedule": schedule,
                          "icon": "⚫", "badge": "offline",
                          "task": "—", "last_seen": "never", "snippet": ""})
            continue

        ts_str, task_type, status, content = row
        try:
            ts = datetime.fromisoformat(ts_str.split("+")[0].split("Z")[0])
            age_s = (now - ts).total_seconds()
            last_seen = ts.strftime("%H:%M:%S")
        except Exception:
            age_s = 9999
            last_seen = str(ts_str)[:19]

        if status == "running":
            icon, badge = "🔵", "running"
        elif status == "error":
            icon, badge = "🔴", "error"
        elif age_s < _ACTIVE_THRESHOLD_S:
            icon, badge = "🟢", "active"
        elif age_s < _IDLE_THRESHOLD_S:
            icon, badge = "🟡", "recent"
        else:
            icon, badge = "⚪", "idle"

        cards.append({
            "name": name, "role": role, "schedule": schedule,
            "icon": icon, "badge": badge,
            "task": task_type or "—",
            "last_seen": last_seen,
            "snippet": str(content or "")[:100],
        })
    conn.close()
    return cards


def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def query(sql: str, params=()) -> pd.DataFrame:
    try:
        conn = get_conn()
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception as e:
        return pd.DataFrame({"error": [str(e)]})


def scalar(sql: str, params=(), default=None):
    try:
        conn = get_conn()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.image("https://upload.wikimedia.org/wikipedia/commons/thumb/4/4e/GameStop_logo.svg/320px-GameStop_logo.svg.png", width=120)
st.sidebar.title("GME Intelligence")
st.sidebar.caption(f"Refreshed: {datetime.now(ET).strftime('%H:%M:%S ET')}")
lookback = st.sidebar.slider("Lookback (days)", 1, 90, 30)
auto_refresh = st.sidebar.checkbox("Auto-refresh (60s)")

# System health indicators in sidebar
st.sidebar.divider()
st.sidebar.caption("SYSTEM STATUS")
tick_count = scalar("SELECT COUNT(*) FROM price_ticks WHERE timestamp > datetime('now','-5 minutes')", default=0)
last_tick  = scalar("SELECT MAX(timestamp) FROM price_ticks", default="—")
st.sidebar.metric("Ticks (5min)", tick_count, delta="live" if tick_count > 0 else "no feed")
st.sidebar.caption(f"Last tick: {str(last_tick)[:19]}")

open_trades = scalar("SELECT COUNT(*) FROM trade_decisions WHERE status='pending' OR status='filled'", default=0)
st.sidebar.metric("Open positions", open_trades)

if auto_refresh:
    st.cache_data.clear()

# ── Account balance in sidebar ────────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.caption("ACCOUNT")
try:
    import yaml
    rules = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "risk_rules.yaml")))
    paper_mode = rules.get("paper_trading", True)
    mode_label = "PAPER" if paper_mode else "⚡ LIVE"
    st.sidebar.caption(f"Mode: **{mode_label}**")

    from broker import get_broker
    acct = get_broker().account_summary()
    if "error" not in acct:
        st.sidebar.metric("Equity (USD)", f"${acct.get('equity_usd', 0):.2f}",
                          delta=f"${acct.get('pnl_today_usd', 0):+.2f} today")
        if acct.get("equity_gbp"):
            st.sidebar.metric("Equity (GBP)", f"£{acct.get('equity_gbp', 0):.2f}")
        st.sidebar.metric("Buying Power", f"${acct.get('buying_power_usd', 0):.2f}")
    else:
        st.sidebar.warning("Alpaca not connected")
except Exception:
    st.sidebar.caption("Set ALPACA_API_KEY to see balance")

# ── Live commentary banner ────────────────────────────────────────────────────
comment_row = query("SELECT comment FROM stream_comments ORDER BY timestamp DESC LIMIT 1")
if not comment_row.empty and "error" not in comment_row.columns:
    st.info(f"🎙️ **Chatty:** {comment_row['comment'].iloc[0]}")

# ── CTO immunity status banner ────────────────────────────────────────────────
red_signals = query(
    "SELECT COUNT(*) as n FROM structural_signals WHERE ticker='GME' AND filing_date >= date('now','-7 days')"
)
_n_signals = int(red_signals["n"].iloc[0]) if (not red_signals.empty and "n" in red_signals.columns) else 0
if _n_signals > 0:
    st.error(f"🚨 **CTO ALERT:** {_n_signals} new GME structural signal(s) detected this week — see CTO Intel tab")
else:
    st.success("✅ **GME Immunity: GREEN** — Zero debt, clean board, no restructuring advisors detected")

# ── Tabs ──────────────────────────────────────────────────────────────────────
(tab_console, tab_price, tab_options, tab_trades, tab_cto,
 tab_social, tab_predictions, tab_logs, tab_quality) = st.tabs([
    "🖥️ Command Console",
    "📊 Price & 1-sec",
    "📈 Options & Max Pain",
    "💹 Trades & P&L",
    "🏛️ CTO Intel",
    "📱 Social Feed",
    "🔮 Predictions",
    "🤖 Agent Logs",
    "✅ Data Quality",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 0: COMMAND CONSOLE
# ══════════════════════════════════════════════════════════════════════════════
with tab_console:
    console_refresh = st.sidebar.checkbox("Console live (10s)", value=False,
                                          key="console_live")

    st.subheader("Agent Status")

    # ── Top-line metrics ───────────────────────────────────────────────────────
    cards = _agent_card_data()
    n_active  = sum(1 for c in cards if c["badge"] in ("active", "running"))
    n_errors  = sum(1 for c in cards if c["badge"] == "error")
    n_idle    = sum(1 for c in cards if c["badge"] in ("idle", "offline"))

    errors_1h = scalar(
        "SELECT COUNT(*) FROM agent_logs WHERE status = 'error' "
        "AND timestamp > datetime('now','-1 hour')", default=0
    )
    trades_today = scalar(
        "SELECT COUNT(*) FROM trade_decisions WHERE timestamp LIKE date('now')||'%'", default=0
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("🟢 Active",    n_active)
    m2.metric("🟡 Recent",    sum(1 for c in cards if c["badge"] == "recent"))
    m3.metric("⚪ Idle",      n_idle)
    m4.metric("🔴 Errors (1h)", errors_1h)
    m5.metric("💹 Trades today", trades_today)

    st.divider()

    # ── Agent status grid (2 columns) ─────────────────────────────────────────
    col_a, col_b = st.columns(2)
    for i, card in enumerate(cards):
        target_col = col_a if i % 2 == 0 else col_b
        with target_col:
            with st.container(border=True):
                h1, h2 = st.columns([3, 1])
                h1.markdown(f"**{card['icon']} {card['name']}** &nbsp; `{card['role']}`")
                h2.caption(card["badge"].upper())
                c1, c2 = st.columns(2)
                c1.caption(f"Task: {card['task']}")
                c2.caption(f"Last seen: {card['last_seen']}")
                if card["snippet"]:
                    st.caption(f"_{card['snippet']}_")

    st.divider()

    # ── Activity heatmap (log count per agent per hour, last 12h) ─────────────
    st.subheader("Activity — Last 12 Hours")
    heat_df = query(
        "SELECT agent_name, strftime('%H:00', timestamp) as hour, COUNT(*) as logs "
        "FROM agent_logs "
        "WHERE timestamp > datetime('now', '-12 hours') "
        "GROUP BY agent_name, hour ORDER BY hour"
    )
    if not heat_df.empty and "error" not in heat_df.columns:
        pivot = heat_df.pivot_table(index="agent_name", columns="hour",
                                    values="logs", fill_value=0)
        def _heat_colour(val):
            if val == 0:
                return "background-color: #f5f5f5; color: #999"
            elif val < 5:
                return "background-color: #c6efce; color: #276221"
            elif val < 15:
                return "background-color: #63be7b; color: #fff"
            else:
                return "background-color: #1a7a3e; color: #fff"

        st.dataframe(
            pivot.style.map(_heat_colour),
            use_container_width=True,
        )
    else:
        st.info("No activity in the last 12 hours.")

    st.divider()

    # ── Live log stream ────────────────────────────────────────────────────────
    st.subheader("Live Log Stream")
    stream_df = query(
        "SELECT timestamp, agent_name, task_type, status, content "
        "FROM agent_logs ORDER BY id DESC LIMIT 25"
    )
    if not stream_df.empty and "error" not in stream_df.columns:
        def _stream_style(row):
            if row["status"] != "ok":
                return ["background-color: #f8d7da"] * len(row)
            if row["agent_name"] == "Chatty":
                return ["background-color: #e8f4fd"] * len(row)
            return [""] * len(row)

        stream_df["content"] = stream_df["content"].str[:120]
        st.dataframe(
            stream_df.style.apply(_stream_style, axis=1),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # ── Quick actions ──────────────────────────────────────────────────────────
    st.subheader("Quick Actions")
    qa1, qa2, qa3, qa4 = st.columns(4)

    if qa1.button("🛑 Halt Trading", use_container_width=True):
        try:
            from notifier import notify
            notify("🛑 <b>HALT</b> issued from dashboard — new trades paused.")
            open(os.path.join(os.path.dirname(__file__), "halt.flag"), "w").close()
            st.warning("Halt flag written. Restart orchestrator to apply if needed.")
        except Exception as e:
            st.error(str(e))

    if qa2.button("▶️ Resume Trading", use_container_width=True):
        try:
            flag = os.path.join(os.path.dirname(__file__), "halt.flag")
            if os.path.exists(flag):
                os.remove(flag)
            from notifier import notify
            notify("▶️ <b>RESUME</b> issued from dashboard — trading re-enabled.")
            st.success("Halt flag removed.")
        except Exception as e:
            st.error(str(e))

    if qa3.button("📋 Trigger Daily Brief", use_container_width=True):
        with st.spinner("Running Briefing Officer..."):
            try:
                from agents import briefing_agent
                from tasks import daily_briefing_task
                from crewai import Crew, Process
                crew = Crew(agents=[briefing_agent], tasks=[daily_briefing_task],
                            process=Process.sequential, verbose=False)
                result = crew.kickoff()
                from notifier import notify
                notify(f"<b>📋 BRIEF (on-demand)</b>\n\n{str(result)[:3000]}")
                st.success("Brief sent to Telegram.")
            except Exception as e:
                st.error(str(e))

    if qa4.button("🔄 Force Sync to Supabase", use_container_width=True):
        with st.spinner("Syncing..."):
            try:
                from supabase_sync import _get_client, _load_state, sync_once, _save_state
                client = _get_client()
                state = _load_state()
                state = sync_once(client, state)
                _save_state(state)
                st.success("Supabase sync complete.")
            except Exception as e:
                st.error(str(e))

    st.divider()
    st.subheader("Force Run Agents")

    def _run_agent(label, fn_path, spinner_msg):
        """Import and call an orchestrator function on demand."""
        import importlib, sys
        load_dotenv(override=True)
        sys.path.insert(0, os.path.dirname(__file__))
        mod = importlib.import_module("orchestrator")
        fn = getattr(mod, fn_path)
        with st.spinner(spinner_msg):
            try:
                fn()
                st.success(f"{label} complete — refresh logs.")
            except Exception as e:
                st.error(str(e))

    r1c1, r1c2, r1c3, r1c4, r1c5 = st.columns(5)
    if r1c1.button("🔍 Valerie", use_container_width=True):
        _run_agent("Valerie", "run_validation", "Validating data...")
    if r1c2.button("💬 Chatty", use_container_width=True):
        _run_agent("Chatty", "run_commentary", "Generating commentary...")
    if r1c3.button("📰 Newsie", use_container_width=True):
        _run_agent("Newsie", "run_news", "Fetching news...")
    if r1c4.button("📐 Pattern", use_container_width=True):
        _run_agent("Pattern", "run_pattern", "Analysing patterns...")
    if r1c5.button("📈 Trendy", use_container_width=True):
        _run_agent("Trendy", "run_daily_trend", "Running trend analysis...")

    r2c1, r2c2, r2c3, r2c4, r2c5 = st.columns(5)
    if r2c1.button("🔮 Futurist", use_container_width=True):
        _run_agent("Futurist", "run_futurist_cycle", "Running strategic cycle...")
    if r2c2.button("🏛️ CTO", use_container_width=True):
        _run_agent("CTO", "run_cto_daily_brief", "Running CTO brief...")
    if r2c3.button("🐦 Social", use_container_width=True):
        _run_agent("Social", "run_social_scan", "Scanning social feeds...")
    if r2c4.button("📋 Briefing", use_container_width=True):
        _run_agent("Briefing", "run_daily_briefing", "Generating brief...")
    if r2c5.button("🔁 Refresh All", use_container_width=True):
        with st.spinner("Running all agents..."):
            import importlib, sys
            sys.path.insert(0, os.path.dirname(__file__))
            mod = importlib.import_module("orchestrator")
            for fn_name in ["run_validation", "run_commentary", "run_news",
                            "run_pattern", "run_daily_trend", "run_futurist_cycle",
                            "run_cto_daily_brief", "run_social_scan"]:
                try:
                    getattr(mod, fn_name)()
                except Exception as e:
                    st.warning(f"{fn_name}: {e}")
            st.success("All agents refreshed.")

    # Live console auto-refresh (10s)
    if console_refresh:
        time.sleep(10)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: PRICE & 1-SEC
# ══════════════════════════════════════════════════════════════════════════════
with tab_price:
    col_left, col_right = st.columns([3, 1])

    with col_left:
        st.subheader("GME Daily Candles")
        price_df = query(
            "SELECT date, open, high, low, close, volume, vwap FROM daily_candles "
            "WHERE symbol='GME' ORDER BY date DESC LIMIT ?", (lookback,)
        )
        if not price_df.empty and "error" not in price_df.columns:
            price_df = price_df.sort_values("date")
            latest = price_df.iloc[-1]
            prev   = price_df.iloc[-2] if len(price_df) > 1 else latest
            pct    = (latest["close"] - prev["close"]) / prev["close"] * 100

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Close", f"${latest['close']:.2f}", f"{pct:+.2f}%")
            m2.metric("High",  f"${latest['high']:.2f}")
            m3.metric("Low",   f"${latest['low']:.2f}")
            m4.metric("VWAP",  f"${latest['vwap']:.2f}" if latest.get("vwap") else "—")
            m5.metric("Volume", f"{int(latest['volume']):,}")

            st.line_chart(price_df.set_index("date")[["close", "vwap"]].dropna())
            st.dataframe(price_df.sort_values("date", ascending=False), use_container_width=True)
        else:
            st.info("No daily candle data yet. Run logger_daemon.py to start collecting.")

    with col_right:
        st.subheader("Today Top / Bottom")
        today_high = scalar(
            "SELECT MAX(high) FROM price_ticks WHERE symbol='GME' AND timestamp LIKE date('now')||'%'", default=0
        )
        today_low = scalar(
            "SELECT MIN(low) FROM price_ticks WHERE symbol='GME' AND timestamp LIKE date('now')||'%'", default=0
        )
        today_vol = scalar(
            "SELECT SUM(volume) FROM price_ticks WHERE symbol='GME' AND timestamp LIKE date('now')||'%'", default=0
        )
        st.metric("Today High", f"${today_high:.2f}" if today_high else "—")
        st.metric("Today Low",  f"${today_low:.2f}"  if today_low  else "—")
        st.metric("Today Volume", f"{int(today_vol):,}" if today_vol else "—")

        if today_high and today_low:
            spread_pct = (today_high - today_low) / today_low * 100
            st.metric("Day Range %", f"{spread_pct:.2f}%")

    st.divider()
    st.subheader("Live 1-Second Ticks (last 120)")
    ticks_df = query(
        "SELECT timestamp, open, high, low, close, volume, source FROM price_ticks "
        "WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 120"
    )
    if not ticks_df.empty and "error" not in ticks_df.columns:
        # Colour-code by source
        def source_style(row):
            if row["source"] == "tradingview":
                return ["background-color: #d4edda"] * len(row)
            elif row["source"] == "alpaca":
                return ["background-color: #cce5ff"] * len(row)
            return [""] * len(row)
        st.dataframe(ticks_df.style.apply(source_style, axis=1), use_container_width=True)
        st.caption("🟢 TradingView (primary 1-sec)  🔵 Alpaca IEX (backup 1-sec)")
    else:
        st.info("No tick data. Start logger_daemon.py and configure TradingView webhook or Alpaca API.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: OPTIONS & MAX PAIN
# ══════════════════════════════════════════════════════════════════════════════
with tab_options:
    st.subheader("Options Intelligence")

    # Latest max pain from DB
    mp_df = query(
        "SELECT timestamp, expiration, max_pain_strike, current_price, delta_to_max_pain, "
        "call_oi_total, put_oi_total, put_call_ratio, net_oi_bias "
        "FROM options_snapshots ORDER BY timestamp DESC LIMIT 8"
    )

    if not mp_df.empty and "error" not in mp_df.columns:
        latest_mp = mp_df.iloc[0]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Max Pain Strike", f"${latest_mp['max_pain_strike']:.2f}",
                    help="Strike where MM losses are minimised — price gravitates here into expiry")
        col2.metric("Current vs Max Pain", f"{latest_mp['delta_to_max_pain']:+.2f}",
                    help="Positive = above max pain (downward MM pressure). Negative = below (upward pressure)")
        col3.metric("Put/Call Ratio", f"{latest_mp['put_call_ratio']:.2f}",
                    help=">1.0 = more put OI (bearish hedging). <1.0 = more call OI (bullish)")
        col4.metric("OI Bias", latest_mp["net_oi_bias"].upper(),
                    help="Which side has more open interest")

        st.caption(f"Expiry: {latest_mp['expiration']}  |  Updated: {str(latest_mp['timestamp'])[:19]}")
        st.divider()

        st.subheader("Max Pain History")
        st.dataframe(mp_df, use_container_width=True)
    else:
        st.info("No options data yet. Options are fetched every Monday at 8:30 AM ET.")
        if st.button("Fetch Now (live)"):
            with st.spinner("Fetching options chain from yfinance..."):
                try:
                    from options_feed import OptionsFeed, ensure_options_table
                    ensure_options_table()
                    feed = OptionsFeed()
                    mp = feed.max_pain()
                    if mp:
                        st.json(mp)
                        feed.update_db(send_telegram=False)
                        st.success("Saved to database.")
                    else:
                        st.error("yfinance returned no data.")
                except Exception as e:
                    st.error(f"Error: {e}")

    st.divider()
    st.subheader("Live Options Chain (fetch on demand)")
    if st.button("Load Options Chain"):
        with st.spinner("Loading from yfinance..."):
            try:
                from options_feed import OptionsFeed
                feed = OptionsFeed()
                exps = feed.get_expirations()
                if exps:
                    exp_choice = st.selectbox("Expiration", exps, index=0)
                    chain = feed.get_chain(exp_choice)
                    if chain:
                        col_c, col_p = st.columns(2)
                        with col_c:
                            st.caption("CALLS")
                            st.dataframe(chain["calls"].set_index("strike"), use_container_width=True)
                        with col_p:
                            st.caption("PUTS")
                            st.dataframe(chain["puts"].set_index("strike"), use_container_width=True)
            except Exception as e:
                st.error(f"Options load failed: {e}")

    st.divider()
    st.subheader("Dark Pool (FINRA ATS)")
    if st.button("Fetch FINRA Dark Pool Data"):
        with st.spinner("Querying FINRA..."):
            try:
                from options_feed import OptionsFeed
                feed = OptionsFeed()
                dp = feed.dark_pool_summary()
                if "error" not in dp:
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Short Volume", f"{dp.get('short_volume', 0):,}")
                    col2.metric("Total Volume",  f"{dp.get('total_volume', 0):,}")
                    col3.metric("Short %",       f"{dp.get('short_pct', 0):.1f}%")
                    st.caption(f"Date: {dp.get('date', '—')}  |  {dp.get('note', '')}")
                    st.info(f"For real-time dark pool: {dp.get('upgrade', '')}")
                else:
                    st.warning(dp.get("error", "No data"))
            except Exception as e:
                st.error(str(e))


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: TRADES & P&L
# ══════════════════════════════════════════════════════════════════════════════
with tab_trades:
    st.subheader("Trade Decisions")
    trades_df = query(
        "SELECT timestamp, action, quantity, entry_price, stop_loss, take_profit, "
        "confidence, status, exit_price, pnl, notes "
        "FROM trade_decisions ORDER BY timestamp DESC LIMIT 100"
    )
    if not trades_df.empty and "error" not in trades_df.columns:
        filled = trades_df[trades_df["status"] == "filled"]
        closed = trades_df[trades_df["status"] == "closed"]
        pnl_data = closed[closed["pnl"].notna()]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total P&L (paper)", f"${pnl_data['pnl'].sum():.2f}")
        col2.metric("Win Rate", f"{(pnl_data['pnl'] > 0).mean():.0%}" if len(pnl_data) > 0 else "—")
        col3.metric("Closed Trades", len(pnl_data))
        col4.metric("Open / Pending", len(filled))

        if not pnl_data.empty:
            st.line_chart(pnl_data.set_index("timestamp")["pnl"].cumsum())

        def row_colour(row):
            if row["status"] == "closed" and pd.notna(row["pnl"]) and row["pnl"] > 0:
                return ["background-color: #d4edda"] * len(row)
            if row["status"] == "closed" and pd.notna(row["pnl"]) and row["pnl"] < 0:
                return ["background-color: #f8d7da"] * len(row)
            if row["status"] == "rejected":
                return ["opacity: 0.5"] * len(row)
            return [""] * len(row)

        st.dataframe(trades_df.style.apply(row_colour, axis=1), use_container_width=True)
    else:
        st.info("No trades yet. Run the orchestrator to generate trade decisions.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: CTO INTEL
# ══════════════════════════════════════════════════════════════════════════════
with tab_cto:
    st.subheader("CTO Market Structure Intelligence")

    # GME immunity checklist
    st.markdown("### GME Immunity Status")
    st.markdown("""
| Check | Condition | Status |
|---|---|---|
| Debt-free | Long-term debt = $0 | ✅ GREEN |
| Cash position | Cash > $1B | ✅ GREEN |
| PE-free board | No Apollo/KKR/Blackstone directors | ✅ GREEN |
| Cohen control | Chairman + >10% stake | ✅ GREEN |
| No restructuring advisor | No AlixPartners/A&M CRO | ✅ GREEN |
| Profitable | TTM net income > $0 | ✅ GREEN |
    """)
    st.caption("Updated manually — integrate EDGAR XBRL for live tracking")

    st.divider()

    # Structural signals
    st.markdown("### PE Playbook Signals Detected")
    signals_df = query(
        "SELECT timestamp, ticker, signal_name, confidence, action, timeline_months, headline "
        "FROM structural_signals ORDER BY timestamp DESC LIMIT 50"
    )
    if not signals_df.empty and "error" not in signals_df.columns:
        # Summary metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Signals (all time)", len(signals_df))
        high_conf = signals_df[signals_df["confidence"] >= 0.85]
        col2.metric("High Confidence (≥85%)", len(high_conf))
        short_signals = signals_df[signals_df["action"] == "SHORT"]
        col3.metric("SHORT Action Signals", len(short_signals))

        # Filter
        ticker_filter = st.selectbox(
            "Filter by ticker", ["All"] + sorted(signals_df["ticker"].unique().tolist())
        )
        if ticker_filter != "All":
            signals_df = signals_df[signals_df["ticker"] == ticker_filter]

        st.dataframe(signals_df, use_container_width=True)
    else:
        st.info("No structural signals yet. CTO EDGAR scan runs Sundays 8 AM ET.")
        if st.button("Run EDGAR Scan Now"):
            with st.spinner("Scanning SEC EDGAR..."):
                try:
                    from sec_scanner import SECScanner
                    scanner = SECScanner()
                    results = scanner.scan_watchlist(days_back=7)
                    if results:
                        st.success(f"Found signals in: {list(results.keys())}")
                        st.rerun()
                    else:
                        st.info("No new signals in the past 7 days.")
                except Exception as e:
                    st.error(str(e))

    st.divider()
    st.markdown("### Short Watchlist")
    watchlist_df = query(
        "SELECT ticker, company_name, signal_score, confidence, action, timeline_months, notes, last_updated "
        "FROM short_watchlist WHERE active=1 ORDER BY signal_score DESC"
    )
    if not watchlist_df.empty and "error" not in watchlist_df.columns:
        st.dataframe(watchlist_df, use_container_width=True)
    else:
        st.info("Short watchlist is empty. Signals from EDGAR scans populate this automatically.")

    st.divider()
    st.markdown("### Latest CTO Briefings")
    cto_logs = query(
        "SELECT timestamp, task_type, content FROM agent_logs "
        "WHERE agent_name='CTO' ORDER BY timestamp DESC LIMIT 10"
    )
    if not cto_logs.empty and "error" not in cto_logs.columns:
        for _, row in cto_logs.iterrows():
            with st.expander(f"[{row['task_type']}] {str(row['timestamp'])[:16]}"):
                st.text(row["content"])
    else:
        st.info("No CTO briefings yet. CTO runs daily at 9:05 AM ET.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: SOCIAL FEED
# ══════════════════════════════════════════════════════════════════════════════
with tab_social:
    st.subheader("Social Intelligence Feed")
    st.caption("Tracking: @ryancohen @larryvc @michaeljburry @TheRoaringKitty")

    social_df = query(
        "SELECT timestamp, username, content, signal_type FROM social_posts ORDER BY timestamp DESC LIMIT 100"
    )
    if not social_df.empty and "error" not in social_df.columns:
        # Metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Posts Tracked", len(social_df))
        col2.metric("Bullish Signals", (social_df["signal_type"] == "BULLISH").sum())
        col3.metric("Bearish Signals", (social_df["signal_type"] == "BEARISH").sum())

        # Filter
        user_filter = st.selectbox(
            "Filter by account", ["All"] + sorted(social_df["username"].unique().tolist())
        )
        if user_filter != "All":
            social_df = social_df[social_df["username"] == user_filter]

        for _, row in social_df.iterrows():
            emoji = {"BULLISH": "🐂", "BEARISH": "🐻", "CRITICAL": "🚨", "INFO": "💬"}.get(row["signal_type"], "💬")
            bg = {"BULLISH": "#d4edda", "BEARISH": "#f8d7da", "CRITICAL": "#fff3cd"}.get(row["signal_type"], "")
            with st.container():
                st.markdown(
                    f"{emoji} **@{row['username']}** `{str(row['timestamp'])[:16]}` "
                    f"— _{row['signal_type']}_\n\n> {row['content'][:400]}"
                )
                st.divider()
    else:
        st.info("No social posts tracked yet. Twitter/X monitor scans every 15 minutes during market hours.")
        if st.button("Scan Now"):
            with st.spinner("Scanning Twitter/X accounts..."):
                try:
                    from twitter_monitor import TwitterMonitor
                    monitor = TwitterMonitor()
                    results = monitor.scan_all()
                    st.success(f"Scanned. Found {len(results)} new posts.")
                    if results:
                        st.rerun()
                except Exception as e:
                    st.error(str(e))

    st.divider()
    st.subheader("Setup: Twitter/X API")
    with st.expander("How to get X_BEARER_TOKEN"):
        st.markdown("""
1. Go to [developer.x.com](https://developer.x.com/en/portal/dashboard)
2. Create a project → create an App
3. In App settings → Keys and Tokens → copy **Bearer Token**
4. Add to `.env`:
   ```
   X_BEARER_TOKEN=AAA...your_token_here
   ```
5. Restart the orchestrator. Social scanning activates automatically.

**Free tier**: 500,000 reads/month — enough for 5 accounts scanned every 15 min.
        """)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6: PREDICTIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_predictions:
    st.subheader("Agent Price Predictions vs Actuals")
    pred_df = query(
        "SELECT timestamp, horizon, predicted_price, confidence, actual_price, error_pct, reasoning "
        "FROM predictions ORDER BY timestamp DESC LIMIT 50"
    )
    if not pred_df.empty and "error" not in pred_df.columns:
        recent = pred_df.head(3)
        cols = st.columns(len(recent))
        for i, (_, row) in enumerate(recent.iterrows()):
            cols[i].metric(
                f"{row['horizon']} horizon",
                f"${row['predicted_price']:.2f}",
                f"conf: {row['confidence']:.0%}",
            )

        # Accuracy over time
        scored = pred_df[pred_df["error_pct"].notna()]
        if not scored.empty:
            st.metric("Avg Prediction Error", f"±{scored['error_pct'].abs().mean():.2f}%")
            st.bar_chart(scored.set_index("timestamp")["error_pct"])

        st.dataframe(pred_df, use_container_width=True)
    else:
        st.info("No predictions yet. Run the orchestrator to generate predictions.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7: AGENT LOGS
# ══════════════════════════════════════════════════════════════════════════════
with tab_logs:
    st.subheader("Agent Activity Logs")
    all_agents = query("SELECT DISTINCT agent_name FROM agent_logs ORDER BY agent_name")
    agent_options = ["All"] + (all_agents["agent_name"].tolist() if not all_agents.empty else [])
    agent_filter = st.selectbox("Filter by agent", agent_options)

    sql = "SELECT agent_name, timestamp, task_type, content, status FROM agent_logs"
    params: tuple = ()
    if agent_filter != "All":
        sql += " WHERE agent_name=?"
        params = (agent_filter,)
    sql += " ORDER BY timestamp DESC LIMIT 200"

    logs_df = query(sql, params)
    if not logs_df.empty and "error" not in logs_df.columns:
        ok   = (logs_df["status"] == "ok").sum()
        errs = (logs_df["status"] == "error").sum()
        st.columns(2)[0].metric("OK", ok)
        st.columns(2)[1].metric("Errors", errs)

        for _, row in logs_df.iterrows():
            icon = "🔵" if row["status"] == "running" else ("✅" if row["status"] == "ok" else "❌")
            with st.expander(f"{icon} [{str(row['timestamp'])[:16]}] {row['agent_name']} — {row['task_type']}"):
                st.text(str(row["content"]))
    else:
        st.info("No agent logs yet.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 8: DATA QUALITY
# ══════════════════════════════════════════════════════════════════════════════
with tab_quality:
    st.subheader("Data Quality (Valerie)")
    quality_df = query(
        "SELECT timestamp, check_type, result, anomalies, status FROM data_quality_logs ORDER BY timestamp DESC LIMIT 100"
    )
    if not quality_df.empty and "error" not in quality_df.columns:
        ok_count  = (quality_df["status"] == "ok").sum()
        err_count = (quality_df["status"] != "ok").sum()
        col1, col2 = st.columns(2)
        col1.metric("Clean checks", ok_count)
        col2.metric("Anomalies", err_count)
        st.dataframe(quality_df, use_container_width=True)

        # Anomaly timeline
        if err_count > 0:
            st.subheader("Anomaly Timeline")
            anomalies = quality_df[quality_df["status"] != "ok"]
            st.dataframe(anomalies, use_container_width=True)
    else:
        st.info("No quality checks yet. Valerie runs every minute once the orchestrator is started.")

    st.divider()
    st.subheader("Learning System")
    perf_df = query(
        "SELECT date, agent_name, metric, value, sample_size FROM performance_scores ORDER BY date DESC LIMIT 50"
    )
    if not perf_df.empty and "error" not in perf_df.columns:
        st.markdown("**Agent Performance Scores**")
        st.dataframe(perf_df, use_container_width=True)

    strat_df = query(
        "SELECT timestamp, parameter, old_value, new_value, reason, reverted "
        "FROM strategy_history ORDER BY timestamp DESC LIMIT 20"
    )
    if not strat_df.empty and "error" not in strat_df.columns:
        st.markdown("**Strategy Changes (Boss-Approved)**")
        st.dataframe(strat_df, use_container_width=True)

# ── Auto-refresh ──────────────────────────────────────────────────────────────
if auto_refresh:
    import time
    time.sleep(60)
    st.rerun()
