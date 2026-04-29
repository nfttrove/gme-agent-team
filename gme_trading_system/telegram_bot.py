"""
Two-way Telegram Bot — command interface for the GME trading system.

Commands:
  /help        — full command guide and chat capabilities
  /status      — system health, agents, tick count
  /standup     — agent daily standup (signals, win rates, ROI)
  /ticks       — price ticks received today
  /freshness   — verify agents are reading current data (not stale tables)
  /agents      — last run time for each agent
  /brief       — today's strategy in plain English
  /update      — sync local data to Supabase immediately
  /supportme   — buy-me-a-coffee / PayPal link
  /frequency   — show current notification frequency
  /frequency low|medium|high — set notification level
               low    = daily summary only
               medium = trades + daily summary (default)
               high   = every agent decision + trades + summary
  /test        — run Telegram handler smoke tests (23 tests, ~1 sec)

Interactive chat: send plain text questions for LLM responses with trading context.
Queries dual curated GameStop research collections via Google Notebook LM first,
then falls back to Gemma/Gemini.

Run as a thread from orchestrator.py.
"""
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

ET = ZoneInfo("America/New_York")

log = logging.getLogger(__name__)

TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH  = os.path.join(os.path.dirname(__file__), "agent_memory.db")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

ENABLED = bool(TOKEN and CHAT_ID)

PAYPAL_URL = "https://www.paypal.com/paypalme/2r0v3"


def _send(text: str):
    if not ENABLED:
        return
    try:
        requests.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        log.warning(f"[tgbot] send failed: {e}")


def _get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{BASE_URL}/getUpdates", params={
            "offset": offset, "timeout": 20
        }, timeout=25)
        return r.json().get("result", [])
    except Exception:
        return []


def _db_scalar(sql: str, params=()):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_frequency() -> str:
    freq = _db_scalar("SELECT value FROM bot_settings WHERE key='notification_frequency'")
    return freq or "medium"


def _set_frequency(level: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)
        """)
        conn.execute("INSERT OR REPLACE INTO bot_settings VALUES ('notification_frequency', ?)", (level,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[tgbot] set_frequency failed: {e}")


def _human_delta(earlier_iso: str, later_iso: str) -> str:
    """Render the gap between two ISO timestamps as '23m' / '1h 5m' / '3d 4h'."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(earlier_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(later_iso.replace("Z", "+00:00"))
        if a.tzinfo is None and b.tzinfo is not None:
            a = a.replace(tzinfo=b.tzinfo)
        elif b.tzinfo is None and a.tzinfo is not None:
            b = b.replace(tzinfo=a.tzinfo)
        secs = max(0, int((b - a).total_seconds()))
    except Exception:
        return "?"
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _ensure_signal_feedback_table():
    """signal_feedback exists in prod but not in every test fixture — create it
    defensively so the /executed /ignored /missed handlers never crash on a
    fresh DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_feedback (
                id TEXT PRIMARY KEY,
                alert_id TEXT NOT NULL,
                action_taken TEXT NOT NULL,
                execution_timestamp TEXT,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                pnl REAL,
                pnl_pct REAL,
                team_member TEXT,
                team_notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT
            )
            """
        )
        # Speed up the dominant lookup: feedback-for-signal.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_feedback_alert "
            "ON signal_feedback(alert_id)"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[tgbot] signal_feedback setup failed: {e}")


def _resolve_signal_short_id(short: str) -> dict | None:
    """Resolve a short UUID prefix (6+ hex chars) to a signal_alerts row.

    Returns:
      None                              — no match
      {'ambiguous': True, 'matches': …} — prefix is too short, multiple hits
      {'ambiguous': False, 'row': …}    — unique match
    """
    short = (short or "").strip().lower().lstrip("#")
    if len(short) < 6:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, agent_name, signal_type, confidence, entry_price, "
            "       stop_loss, take_profit, reasoning, timestamp "
            "FROM signal_alerts WHERE lower(id) LIKE ? LIMIT 5",
            (short + "%",),
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"[tgbot] signal lookup failed: {e}")
        return None
    if not rows:
        return None
    if len(rows) > 1:
        return {"ambiguous": True, "matches": [dict(r) for r in rows]}
    return {"ambiguous": False, "row": dict(rows[0])}


def _record_signal_feedback(signal_id: str, action: str, notes: str,
                            team_member: str) -> dict | None:
    """Upsert a signal_feedback row, keyed by (alert_id, action_taken).

    Idempotent on that key — running /executed twice on the same signal
    updates team_notes/team_member, doesn't create a duplicate. Different
    actions on the same signal (e.g., /executed then /ignored) create
    separate rows so the history of the decision-change is visible.
    """
    import uuid
    _ensure_signal_feedback_table()
    now = datetime.now(ET).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT id FROM signal_feedback "
            "WHERE alert_id=? AND action_taken=? LIMIT 1",
            (signal_id, action),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE signal_feedback SET team_notes=?, team_member=?, "
                "updated_at=? WHERE id=?",
                (notes, team_member, now, existing["id"]),
            )
            fb_id = existing["id"]
        else:
            fb_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO signal_feedback "
                "(id, alert_id, action_taken, execution_timestamp, "
                " team_member, team_notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fb_id, signal_id, action, now, team_member, notes, now, now),
            )
        conn.commit()
        conn.close()
        return {"id": fb_id, "alert_id": signal_id, "action": action,
                "team_notes": notes, "team_member": team_member}
    except Exception as e:
        log.error(f"[tgbot] feedback insert failed: {e}")
        return None


def _ensure_settings_table():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)
        """)
        conn.execute("INSERT OR IGNORE INTO bot_settings VALUES ('notification_frequency', 'medium')")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _run_agent_refresh():
    """Run key agents and collect their outputs for a quick system refresh."""
    results = {}
    try:
        from crewai import Crew, Process
        from agents import valerie_agent, synthesis_agent, news_analyst_agent, cto_agent
        from tasks import make_validate_data_task, make_synthesis_task, news_task, cto_daily_brief_task
        from market_state import get_market_fact

        # Single source of truth for price direction
        fact = get_market_fact("GME", DB_PATH)

        # Pre-fetch all live data in one pass
        conn = sqlite3.connect(DB_PATH)
        price_row = conn.execute(
            "SELECT close, volume, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        latest_ts_row = conn.execute(
            "SELECT timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        tick_count = conn.execute(
            "SELECT COUNT(*) FROM price_ticks WHERE symbol='GME' AND datetime(timestamp) > datetime('now','-5 minutes')"
        ).fetchone()[0]
        agent_logs = conn.execute(
            "SELECT agent_name, task_type, content, timestamp FROM agent_logs "
            "WHERE datetime(timestamp) > datetime('now', '-2 hours') ORDER BY timestamp DESC LIMIT 40"
        ).fetchall()
        structural = conn.execute(
            "SELECT content FROM agent_logs WHERE task_type='investor_intel' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        news_rows = conn.execute(
            "SELECT headline, sentiment_score FROM news_analysis ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        conn.close()

        # Inject market fact into price_str so Synthesis/Valerie see direction
        price_str = (
            f"${price_row[0]:.2f} {fact['direction'].lower()} "
            f"({fact['pct_change']:+.2f}% vs yesterday's ${fact['prev_close']:.2f if fact['prev_close'] else 'N/A'}) "
            f"(volume: {int(price_row[1] or 0)}, as of {price_row[2][:16]})"
        ) if price_row and fact['prev_close'] else (
            f"${price_row[0]:.2f} (volume: {int(price_row[1] or 0)}, as of {price_row[2][:16]})"
            if price_row else "unavailable"
        )
        latest_ts = latest_ts_row[0][:19] if latest_ts_row else "never"
        agent_logs_str = "\n".join(f"  [{r[3][:16]}] {r[0]} ({r[1]}): {str(r[2])[:150]}" for r in agent_logs) if agent_logs else "  No recent logs."

        # Valerie — data quality check (fast)
        try:
            task = make_validate_data_task(valerie_agent, tick_count, latest_ts, 0, 0)
            crew = Crew(agents=[valerie_agent], tasks=[task],
                       process=Process.sequential, verbose=False)
            result = crew.kickoff()
            results['valerie'] = str(result)[:300]
            log.info("[tgbot] Valerie report collected")
        except Exception as e:
            results['valerie'] = f"Error: {str(e)[:100]}"

        # Synthesis — team consensus with live data
        try:
            task = make_synthesis_task(synthesis_agent, price_str, agent_logs_str)
            crew = Crew(agents=[synthesis_agent], tasks=[task],
                       process=Process.sequential, verbose=False)
            result = crew.kickoff()
            results['synthesis'] = str(result)[:300]
            log.info("[tgbot] Synthesis report collected")
        except Exception as e:
            results['synthesis'] = f"Error: {str(e)[:100]}"

        # News sentiment (fast)
        try:
            from market_hours import is_market_open
            if is_market_open():
                crew = Crew(agents=[news_analyst_agent], tasks=[news_task],
                           process=Process.sequential, verbose=False)
                result = crew.kickoff()
                results['news'] = str(result)[:300]
                log.info("[tgbot] News report collected")
            else:
                results['news'] = "Market closed — skipping news scan"
        except Exception as e:
            results['news'] = f"Error: {str(e)[:100]}"

        # CTO brief — inject pre-fetched news and investor intel
        try:
            structural_str = structural[0][:300] if structural else "No investor intel logged."
            news_str = "\n".join(f"  {r[0][:80]} (score: {r[1]})" for r in news_rows) if news_rows else "  No recent news."
            from crewai import Task as _Task
            cto_task = _Task(
                description=(
                    cto_daily_brief_task.description.split("STEP 3")[0] +
                    f"STEP 3 — ANTI-PATTERN ALERTS\nLIVE NEWS (do not invent):\n{news_str}\n\n"
                    f"STEP 4 — KEY INVESTOR INTELLIGENCE\nLIVE DATA:\n{structural_str}\n\n"
                    "STEP 5 — STATE STRUCTURAL BIAS: BULLISH / BEARISH / NEUTRAL\n\n"
                    "Output in the format specified."
                ),
                expected_output=cto_daily_brief_task.expected_output,
                agent=cto_agent,
            )
            crew = Crew(agents=[cto_agent], tasks=[cto_task],
                       process=Process.sequential, verbose=False)
            result = crew.kickoff()
            results['cto'] = str(result)[:300]
            log.info("[tgbot] CTO report collected")
        except Exception as e:
            results['cto'] = f"Error: {str(e)[:100]}"

    except Exception as e:
        log.error(f"[tgbot] Agent refresh failed: {e}")
        results['error'] = str(e)

    return results


def handle_command(text: str, user: str = "team"):
    """Dispatch a slash-command. `user` is the Telegram username (or
    first_name) captured at poll time — logged against /executed, /ignored,
    /missed so we know who made the call. Defaults to 'team' for tests and
    for messages where Telegram didn't include a from_user field."""
    cmd = text.strip().lower().split()[0]
    args = text.strip().split()[1:] if len(text.strip().split()) > 1 else []

    if cmd == "/update":
        _send("⏳ Running system refresh — agents updating...")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))

            # Run agent refresh
            agent_results = _run_agent_refresh()

            # Build response
            msg_lines = ["<b>📊 SYSTEM REFRESH COMPLETE</b>\n"]
            msg_lines.append(f"<b>Data Validator (Valerie):</b>\n{agent_results.get('valerie', 'N/A')[:200]}\n")
            msg_lines.append(f"<b>Team Consensus (Synthesis):</b>\n{agent_results.get('synthesis', 'N/A')[:200]}\n")
            msg_lines.append(f"<b>News Sentiment:</b>\n{agent_results.get('news', 'N/A')[:200]}\n")
            msg_lines.append(f"<b>Structural Intel (CTO):</b>\n{agent_results.get('cto', 'N/A')[:200]}\n")

            _send("\n".join(msg_lines))

            # Also sync Supabase
            try:
                from supabase_sync import _get_client, _load_state, sync_once
                client = _get_client()
                state = _load_state()
                state = sync_once(client, state)
                _send("✅ <b>Supabase sync complete.</b>")
                log.info("[tgbot] Manual /update with agent refresh triggered")
            except Exception as e:
                log.error(f"[tgbot] Supabase sync failed: {e}")
                _send(f"⚠️ Sync warning: {str(e)[:100]}")

        except Exception as e:
            _send(f"❌ Refresh failed: {str(e)[:200]}")
            log.error(f"[tgbot] Update command failed: {e}")

    elif cmd == "/status":
        tick_count = _db_scalar("SELECT COUNT(*) FROM price_ticks WHERE date(timestamp)=date('now')")
        last_log   = _db_scalar("SELECT agent_name || ': ' || task_type FROM agent_logs ORDER BY timestamp DESC LIMIT 1")
        freq       = _get_frequency()
        _send(
            f"<b>GME System Status</b>\n"
            f"Ticks today: {tick_count or 0}\n"
            f"Notifications: {freq}\n"
            f"Last agent: {last_log or 'none yet'}\n"
            f"Time: {datetime.now(ET).strftime('%H:%M:%S')}"
        )

    elif cmd == "/ticks":
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT close, timestamp FROM price_ticks ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            last_24h = conn.execute(
                "SELECT COUNT(*) FROM price_ticks WHERE datetime(timestamp) > datetime('now', '-1 day')"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM price_ticks").fetchone()[0]
            conn.close()
            if row:
                last_ts = row[1][:16].replace("T", " ")
                _send(
                    f"<b>GME Tick Data</b>\n"
                    f"Latest price: <b>${row[0]:.2f}</b>\n"
                    f"Last tick: {last_ts} UTC\n"
                    f"Ticks (24h): {last_24h}\n"
                    f"Total in DB: {total}"
                )
            else:
                _send("No price ticks in database yet.")
        except Exception as e:
            _send(f"Ticks error: {e}")

    elif cmd == "/freshness":
        try:
            import data_freshness
            results = data_freshness.check()
            lines = ["<b>Data Freshness</b>"]
            any_bad = False
            for name, ok, detail in results:
                icon = "✅" if ok else "❌"
                if not ok:
                    any_bad = True
                lines.append(f"{icon} <code>{name}</code>\n   {detail}")
            verdict = "⚠️ Stale data — agent narratives may be wrong." if any_bad else "All green — safe to trust agent output."
            lines.append("")
            lines.append(f"<b>{verdict}</b>")
            _send("\n".join(lines))
        except Exception as e:
            _send(f"Freshness error: {e}")

    elif cmd == "/agents":
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute("""
                SELECT agent_name, task_type, status, MAX(timestamp)
                FROM agent_logs
                GROUP BY agent_name
                ORDER BY MAX(timestamp) DESC
                LIMIT 10
            """).fetchall()
            conn.close()
            if not rows:
                _send("No agent logs yet.")
                return
            lines = ["<b>Agent Last Activity</b>"]
            for name, task, status, ts in rows:
                icon = "✅" if status == "ok" else "❌"
                ts_short = ts[:16] if ts else "?"
                lines.append(f"{icon} <b>{name}</b> [{task}] {ts_short}")
            _send("\n".join(lines))
        except Exception as e:
            _send(f"Agent log error: {e}")

    elif cmd == "/standup":
        try:
            conn = sqlite3.connect(DB_PATH)

            # Agent activity last 24h
            activity = conn.execute("""
                SELECT agent_name,
                       COUNT(*) as runs,
                       SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) as ok_count
                FROM agent_logs
                WHERE datetime(timestamp) > datetime('now', '-1 day')
                GROUP BY agent_name
                ORDER BY runs DESC
            """).fetchall()

            # Latest price
            price_row = conn.execute(
                "SELECT close, timestamp FROM price_ticks ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

            # Latest prediction
            pred = conn.execute(
                "SELECT horizon, predicted_price, confidence FROM predictions ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

            # Futurist calibration — the truth behind the "confidence" number.
            # Pull latest performance_scores row for each metric.
            calib = {}
            for metric in ("prediction_mae_pct", "direction_hit_rate", "brier_score"):
                row = conn.execute(
                    "SELECT value, sample_size, date FROM performance_scores "
                    "WHERE agent_name='Futurist' AND metric=? "
                    "ORDER BY date DESC, id DESC LIMIT 1",
                    (metric,),
                ).fetchone()
                if row:
                    calib[metric] = row

            # Recent trade decisions (last 7 days)
            trades = conn.execute("""
                SELECT action, COUNT(*) as n, AVG(confidence) as avg_conf
                FROM trade_decisions
                WHERE datetime(timestamp) > datetime('now', '-7 days')
                GROUP BY action
            """).fetchall()

            conn.close()

            lines = ["<b>🤖 AGENT STANDUP (last 24h)</b>\n"]

            if price_row:
                age = price_row[1][:16].replace("T", " ")
                lines.append(f"<b>GME:</b> ${price_row[0]:.2f} (as of {age})\n")

            if pred:
                conf_pct = int(pred[2] * 100) if pred[2] else 0
                lines.append(f"<b>Futurist call:</b> {pred[0]} → ${pred[1]:.2f} <i>(stated {conf_pct}%)</i>")

                # Reality-check the stated confidence against actual track record
                if calib.get("direction_hit_rate") and calib.get("brier_score"):
                    hit = calib["direction_hit_rate"][0]
                    n = calib["direction_hit_rate"][1]
                    brier = calib["brier_score"][0]
                    mae = calib.get("prediction_mae_pct", [0])[0] or 0
                    # Brier < 0.25 beats random; > 0.25 is worse than a coin flip
                    verdict = "📉 worse than random" if brier > 0.25 else "📈 beats random"
                    lines.append(
                        f"<b>Futurist reality (7d, n={n}):</b>\n"
                        f"  • Hit rate: {hit:.0%}  |  MAE: {mae:.2f}%  |  Brier: {brier:.3f} ({verdict})"
                    )
                else:
                    lines.append("  <i>Calibration: not enough scored predictions yet</i>")
                lines.append("")

            # Signal-based calibration for Pattern / Trendy (and Futurist's
            # signal_alerts row, which is scored by the 4h first-touch
            # framework rather than horizon price-regression). These come
            # from signal_scores → performance_scores, one row per metric.
            sig_conn = sqlite3.connect(DB_PATH)
            sig_rows = sig_conn.execute(
                """
                SELECT agent_name, metric, value, sample_size, date
                FROM performance_scores
                WHERE metric IN ('direction_hit_rate','brier_score','tp_hit_rate')
                  AND agent_name IN ('Pattern','Trendy')
                  AND notes LIKE 'source=signal_alerts%'
                  AND date >= date('now','-7 days')
                ORDER BY agent_name, date DESC, id DESC
                """
            ).fetchall()
            sig_conn.close()
            # Collapse to the most recent row per (agent, metric)
            sig_by_agent: dict[str, dict] = {}
            for agent, metric, value, n, _date in sig_rows:
                bucket = sig_by_agent.setdefault(agent, {})
                if metric not in bucket:
                    bucket[metric] = (value, n)
            if sig_by_agent:
                lines.append("<b>Signal agents reality (7d, first-touch 4h):</b>")
                for agent in ("Pattern", "Trendy"):
                    m = sig_by_agent.get(agent)
                    if not m:
                        continue
                    hit = m.get("direction_hit_rate", (None, 0))
                    tp  = m.get("tp_hit_rate",       (None, 0))
                    br  = m.get("brier_score",       (None, 0))
                    n   = hit[1] or tp[1] or br[1] or 0
                    if n == 0:
                        continue
                    verdict = ""
                    if br[0] is not None:
                        verdict = " 📉 worse than random" if br[0] > 0.25 else " 📈 beats random"
                    parts = [f"<b>{agent}</b> (n={n}):"]
                    if hit[0] is not None:
                        parts.append(f"hit {hit[0]:.0%}")
                    if tp[0] is not None:
                        parts.append(f"TP {tp[0]:.0%}")
                    if br[0] is not None:
                        parts.append(f"Brier {br[0]:.2f}{verdict}")
                    lines.append("  • " + "  |  ".join(parts))
                lines.append("")

            if activity:
                lines.append("<b>Agent Runs:</b>")
                for name, runs, ok in activity:
                    ok = ok or 0
                    icon = "✅" if ok == runs else "⚠️"
                    lines.append(f"  {icon} <b>{name}</b>: {runs} runs ({ok} ok)")
            else:
                lines.append("No agent activity in last 24h.")

            if trades:
                lines.append("\n<b>Trade Gate (7d):</b>")
                for action, n, avg_conf in trades:
                    conf_pct = int(avg_conf * 100) if avg_conf else 0
                    lines.append(f"  {action.upper()}: {n}x @ {conf_pct}% avg confidence")

            _send("\n".join(lines))
        except Exception as e:
            _send(f"Standup error: {e}")
            log.error(f"[tgbot] /standup failed: {e}")

    elif cmd == "/signals":
        # List recent signals with their short IDs so the team can
        # reference them in /executed /ignored /missed.
        try:
            _ensure_signal_feedback_table()
            n = 10
            if args and args[0].isdigit():
                n = max(1, min(25, int(args[0])))
            conn = sqlite3.connect(DB_PATH)
            # Tables may not exist in a fresh environment — guard with a
            # schema check before querying so we fail explanatorily.
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_alerts'"
            ).fetchone()
            if not has_table:
                conn.close()
                _send("No signal_alerts table yet — run a signal cycle first.")
                return
            rows = conn.execute(
                f"""
                SELECT s.id, s.agent_name, s.signal_type, s.confidence,
                       s.entry_price, s.take_profit, s.stop_loss, s.timestamp,
                       (SELECT group_concat(action_taken, ',')
                          FROM signal_feedback WHERE alert_id = s.id) AS feedback
                FROM signal_alerts s
                ORDER BY s.timestamp DESC
                LIMIT {n}
                """
            ).fetchall()
            conn.close()
            if not rows:
                _send("No signals in the log yet.")
                return
            lines = [
                f"<b>📋 RECENT SIGNALS (last {len(rows)})</b>",
                "<i>Copy short ID, then /executed &lt;id&gt; · /ignored &lt;id&gt; "
                "[reason] · /missed &lt;id&gt; [note]</i>\n",
            ]
            for (sig_id, agent, stype, conf, entry, tp, sl, ts, fb) in rows:
                short = (sig_id or "")[:8]
                entry_v = entry or 0.0
                tp_v = tp or 0.0
                sl_v = sl or 0.0
                direction = "🟢" if tp_v > entry_v else "🔴" if tp_v < entry_v else "⚪"
                time_str = (ts or "")[:16].replace("T", " ")
                conf_pct = int((conf or 0) * 100)
                fb_tag = f"  ✅ {fb}" if fb else ""
                lines.append(
                    f"<code>{short}</code> {direction} <b>{agent}</b> "
                    f"conf={conf_pct}% · ${entry_v:.2f} → TP ${tp_v:.2f} / "
                    f"SL ${sl_v:.2f}  <i>{time_str}</i>{fb_tag}"
                )
            _send("\n".join(lines))
        except Exception as e:
            _send(f"Signals error: {str(e)[:200]}")
            log.error(f"[tgbot] /signals failed: {e}")

    elif cmd in ("/executed", "/ignored", "/missed"):
        # Close the feedback loop — team logs what they did with a signal.
        # This populates signal_feedback so the calibrator can later compute
        # real team-ROI (execute/ignore rate per agent, PnL on executed).
        action_map = {"/executed": "executed",
                      "/ignored":  "ignored",
                      "/missed":   "missed"}
        action = action_map[cmd]
        if not args:
            _send(
                f"<b>Usage:</b> <code>{cmd} &lt;short_id&gt; [note/reason]</code>\n"
                f"See /signals for IDs."
            )
            return
        short_id = args[0]
        notes = " ".join(args[1:]) if len(args) > 1 else ""
        match = _resolve_signal_short_id(short_id)
        if match is None:
            _send(
                f"⚠️ No signal matching <code>{short_id}</code>. "
                f"Use /signals to list recent IDs."
            )
            return
        if match.get("ambiguous"):
            cand = "\n".join(
                f"  <code>{m['id'][:12]}</code> — {m['agent_name']} "
                f"({m.get('signal_type','?')}) at {(m.get('timestamp','') or '')[:16]}"
                for m in match["matches"]
            )
            _send(
                f"⚠️ <code>{short_id}</code> matches multiple signals:\n{cand}\n"
                f"<i>Add more characters to disambiguate.</i>"
            )
            return
        row = match["row"]
        fb = _record_signal_feedback(row["id"], action, notes, user)
        if not fb:
            _send("❌ Failed to record feedback (DB write error). Check logs.")
            return
        direction = "🟢" if (row.get("take_profit") or 0) > (row.get("entry_price") or 0) else "🔴"
        entry_v = row.get("entry_price") or 0.0
        tp_v = row.get("take_profit") or 0.0
        conf_pct = int((row.get("confidence") or 0) * 100)
        note_line = f"\n<b>Notes:</b> {notes}" if notes else ""
        _send(
            f"✅ <b>{action.upper()}</b> recorded by <b>{user}</b>\n"
            f"{direction} <b>{row['agent_name']}</b> · conf {conf_pct}% · "
            f"entry ${entry_v:.2f} → TP ${tp_v:.2f}\n"
            f"<code>{row['id'][:8]}</code>{note_line}"
        )

    elif cmd in ("/supportme", "/buymeacoffee"):
        _send(
            "☕ <b>Support the team</b>\n\n"
            "If this bot has been useful, a coffee keeps it brewing:\n"
            f"👉 <a href=\"{PAYPAL_URL}\">{PAYPAL_URL}</a>\n\n"
            "<i>No pressure — the signals stay free either way.</i>"
        )

    elif cmd == "/brief":
        _send("⏳ Generating strategy brief — takes ~30 seconds...")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from crewai import Crew, Process, Task
            from agents import briefing_agent
            from market_state import get_market_fact, enforce_direction, enforce_levels

            # Single source of truth for price direction
            fact = get_market_fact("GME", DB_PATH)

            conn = sqlite3.connect(DB_PATH)
            agent_logs = conn.execute(
                "SELECT agent_name, task_type, content, timestamp FROM agent_logs ORDER BY timestamp DESC LIMIT 5"
            ).fetchall()
            safety = conn.execute(
                "SELECT content FROM agent_logs WHERE agent_name='SafetyGate' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            conn.close()

            logs_str = "\n".join(
                f"  [{r[3][:16]}] {r[0]} ({r[1]}): {str(r[2])[:120]}" for r in agent_logs
            ) if agent_logs else "  No recent agent logs."
            safety_str = safety[0][:200] if safety else "No safety gate result."

            today_low = fact.get('today_low')
            today_high = fact.get('today_high')
            r5_low = fact.get('range_5d_low')
            r5_high = fact.get('range_5d_high')
            range_str = (
                f"${today_low:.2f} to ${today_high:.2f}"
                if today_low is not None else "[not available]"
            )
            sr_rule = (
                f"Support/resistance MUST fall inside the 5-day range "
                f"${r5_low:.2f}–${r5_high:.2f}. Do NOT invent round numbers outside it."
                if r5_low is not None else
                "Only cite support/resistance from verified price data."
            )
            live_task = Task(
                description=(
                    f"Produce a plain-English strategy briefing for the CEO. No jargon.\n\n"
                    f"{fact['prompt_line']}\n\n"
                    f"Recent agent logs:\n{logs_str}\n"
                    f"Safety gate: {safety_str}\n\n"
                    "Write EXACTLY this format — use the MARKET FACT verbatim, do not invent direction or levels:\n\n"
                    f"📍 MARKET: GME is at ${fact['price']:.2f}. It is {fact['direction'].lower()} today.\n\n"
                    "📐 PATTERN: [Describe any pattern in plain English.]\n\n"
                    f"🎯 KEY LEVELS: Support at $[X]. Resistance at $[Y]. Today's range: {range_str}.\n\n"
                    "⏳ WAITING FOR: [What signal the system needs before placing a trade.]\n\n"
                    "⚠️ RISK: [One thing that would stop today's plan.]\n\n"
                    "🔮 CONFIDENCE: [X]% — [one sentence on why]\n\n"
                    f"RULES:\n"
                    f"- Today's range is already filled in above — DO NOT change it.\n"
                    f"- {sr_rule}"
                ),
                expected_output="A structured 6-section strategy brief using the live data provided.",
                agent=briefing_agent,
            )
            crew = Crew(agents=[briefing_agent], tasks=[live_task],
                        process=Process.sequential, verbose=False)
            result = crew.kickoff()

            # Safety net: enforce correct direction + verified ranges
            result_str = enforce_direction(str(result), fact)
            result_str = enforce_levels(result_str, fact)
            _send(f"<b>📋 STRATEGY BRIEF</b>\n\n{result_str[:3000]}")
        except Exception as e:
            _send(f"Brief failed: {e}")

    elif cmd == "/frequency":
        levels = {"low", "medium", "high"}
        if args and args[0].lower() in levels:
            level = args[0].lower()
            _set_frequency(level)
            descriptions = {
                "low":    "Daily summary only",
                "medium": "Trades + daily summary",
                "high":   "Every agent decision + trades + summary",
            }
            _send(f"Notification frequency set to <b>{level}</b>.\n{descriptions[level]}")
        else:
            current = _get_frequency()
            _send(
                f"Current frequency: <b>{current}</b>\n\n"
                f"Change with:\n"
                f"/frequency low — daily summary only\n"
                f"/frequency medium — trades + daily summary\n"
                f"/frequency high — every agent decision"
            )

    elif cmd == "/learn":
        # /learn "claim" --why "rationale"
        import subprocess, sys
        import os as os_module
        agent_dir = os_module.path.dirname(__file__)
        learn_script = os_module.path.join(agent_dir, "..", ".agent", "tools", "learn.py")

        try:
            full_text = text.strip()
            if "--why" not in full_text:
                _send("❌ Usage: /learn \"<lesson>\" --why \"<reason>\"\nExample: /learn \"High IV = premium decay\" --why \"IV rank > 70% = better theta\"")
                return

            parts = full_text.split("--why", 1)
            claim = parts[0].replace("/learn", "").strip().strip('"\'')
            why = parts[1].strip().strip('"\'') if len(parts) > 1 else ""

            if not claim or not why:
                _send("❌ Both claim and reason required.")
                return

            # Run learn.py
            result = subprocess.run(
                [sys.executable, learn_script, claim, "--why", why],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0:
                _send(f"✅ <b>Lesson graduated!</b>\n\n<i>{claim}</i>\n\nWhy: {why}")
                log.info(f"[tgbot] Lesson learned: {claim}")
            else:
                _send(f"⚠️ Learn failed: {result.stderr[:200]}")
        except Exception as e:
            _send(f"❌ Error: {str(e)[:200]}")
            log.error(f"[tgbot] /learn failed: {e}")

    elif cmd == "/trove":
        tickers = [a.strip("'\"/").upper() for a in args if a.strip("'\"/")] if args else None
        label   = " ".join(tickers) if tickers else "default watchlist"
        _send(f"⏳ Running Trove Score on <b>{label}</b>…")
        try:
            import sys, os as _os
            sys.path.insert(0, _os.path.dirname(__file__))
            from trove import run_screen, DEFAULT_WATCHLIST
            ticker_list = tickers if tickers else DEFAULT_WATCHLIST
            results = run_screen(ticker_list, max_tickers=20)
            if not results:
                _send("❌ No data returned — check ticker symbols.")
            else:
                lines = ["<b>📊 Trove Score Rankings</b>\n"]
                for r in results:
                    imm    = "🛡️" * r["immunity"]
                    lines.append(
                        f"<b>{r['ticker']}</b>  {r['score']:.1f}/100  {r['rating']}  {imm}\n"
                        f"  A={r['pillar_A']:.0f}/30  B={r['pillar_B']:.0f}/45  C={r['pillar_C']:.0f}/25  "
                        f"NetCash {r['net_cash_pct']}%  AltZ {r['altman_z']}"
                    )
                lines.append("\n<i>A=Valuation · B=Capital · C=Quality</i>")
                _send("\n".join(lines))
        except Exception as e:
            _send(f"❌ Trove error: {str(e)[:200]}")
            log.error(f"[tgbot] /trove failed: {e}")

    elif cmd == "/lessons":
        import subprocess, sys
        import os as os_module
        agent_dir = os_module.path.dirname(__file__)
        recall_script = os_module.path.join(agent_dir, "..", ".agent", "tools", "recall.py")

        try:
            # Get query from args
            query = " ".join(args) if args else "trading strategy"
            result = subprocess.run(
                [sys.executable, recall_script, query],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0 and result.stdout:
                _send(f"<b>📚 Lessons for: {query}</b>\n\n{result.stdout[:2000]}")
            else:
                _send(f"No lessons found for: {query}\n\nTeach one with: /learn \"<lesson>\" --why \"<reason>\"")
        except Exception as e:
            _send(f"❌ Recall error: {str(e)[:200]}")
            log.error(f"[tgbot] /lessons failed: {e}")

    elif cmd == "/force":
        agent_map = {
            "valerie":   ("run_validation",                "Valerie (data validation)"),
            "chatty":    ("run_commentary",                "Chatty (commentary)"),
            "newsie":    ("run_news",                      "Newsie (news sentiment)"),
            "pattern":   ("run_pattern",                   "Pattern (chart patterns)"),
            "trendy":    ("run_daily_trend",               "Trendy (daily trend)"),
            "futurist":  ("run_futurist_prediction_signal","Futurist (price prediction)"),
            "georisk":   ("run_georisk",                   "GeoRisk (geopolitical)"),
            "synthesis": ("run_synthesis",                 "Synthesis (team consensus)"),
            "boss":      ("run_daily_huddle",              "Boss (daily mission briefing)"),
            "cto":       ("run_cto_daily_brief",           "CTO (short-side research)"),
        }
        if not args or args[0].lower() not in agent_map:
            names = ", ".join(sorted(agent_map))
            _send(
                "<b>Force an agent cycle</b>\n\n"
                f"Usage: /force &lt;agent&gt;\nAgents: {names}\n\n"
                "Runs the agent once on demand. Takes 10–60s depending on LLM."
            )
        else:
            key = args[0].lower()
            func_name, label = agent_map[key]

            # Snapshot the most recent prior run BEFORE we trigger a new one,
            # so we can show "previous run was X ago" in the reply.
            prev_ts = None
            try:
                conn = sqlite3.connect(DB_PATH)
                prev_row = conn.execute(
                    "SELECT timestamp FROM agent_logs "
                    "WHERE lower(agent_name) = ? AND status='ok' "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (key,),
                ).fetchone()
                conn.close()
                if prev_row:
                    prev_ts = prev_row[0]
            except Exception:
                pass

            _send(f"⏳ Forcing {label}…")
            try:
                import orchestrator
                func = getattr(orchestrator, func_name)
                # /force is explicitly user-initiated — bypass any
                # @market_hours_required / @active_window_required gates so
                # it works overnight, on weekends, etc. Fully unwrap because
                # some run_* are stacked with both decorators.
                inner = func
                while hasattr(inner, "__wrapped__"):
                    inner = inner.__wrapped__
                inner()
                # Report the log line the agent just wrote
                conn = sqlite3.connect(DB_PATH)
                row = conn.execute(
                    "SELECT content, status, timestamp FROM agent_logs "
                    "WHERE lower(agent_name) = ? ORDER BY timestamp DESC LIMIT 1",
                    (key,),
                ).fetchone()
                conn.close()
                if row:
                    content, status, ts = row
                    icon = "✅" if status == "ok" else "⚠️"
                    header = f"{icon} <b>{label}</b> [{status}]"
                    ts_short = ts[:19].replace("T", " ") if ts else "?"
                    lines = [header, f"Ran: {ts_short}"]
                    if prev_ts and prev_ts != ts:
                        prev_short = prev_ts[:19].replace("T", " ")
                        delta = _human_delta(prev_ts, ts)
                        lines.append(f"Previous: {prev_short} ({delta} ago)")
                    elif not prev_ts:
                        lines.append("Previous: never (first run on record)")
                    lines.append("")
                    lines.append(f"<i>{content[:1400]}</i>")
                    _send("\n".join(lines))
                else:
                    _send(f"✅ {label} ran — no new log row (check /status)")
            except Exception as e:
                log.error(f"[tgbot] /force {key} failed: {e}")
                _send(f"❌ /force {key} failed: {str(e)[:400]}")

    elif cmd == "/swot":
        _send("⏳ Building SWOT — aggregating agent intel (~20s)…")
        try:
            from market_state import get_market_fact
            import requests as _req

            fact = get_market_fact("GME", DB_PATH)

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            agents = ("Valerie", "Chatty", "Newsie", "Pattern", "Trendy",
                      "Futurist", "GeoRisk", "Synthesis", "CTO")
            agent_lines = []
            for name in agents:
                row = conn.execute(
                    "SELECT content, timestamp FROM agent_logs "
                    "WHERE agent_name=? AND status='ok' "
                    "AND timestamp > datetime('now','-24 hours') "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (name,),
                ).fetchone()
                if row and row["content"]:
                    snippet = row["content"][:220].replace("\n", " ").strip()
                    agent_lines.append(f"  {name}: {snippet}")

            # News sentiment tally (last 24h)
            news_rows = conn.execute(
                "SELECT sentiment_label, COUNT(*) FROM news_analysis "
                "WHERE timestamp > datetime('now','-24 hours') "
                "GROUP BY sentiment_label"
            ).fetchall()
            news_tally = ", ".join(f"{r[0]}: {r[1]}" for r in news_rows) or "no recent news"
            top_headline = conn.execute(
                "SELECT headline, sentiment_label FROM news_analysis "
                "WHERE timestamp > datetime('now','-24 hours') "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

            # Latest prediction
            pred = conn.execute(
                "SELECT horizon, predicted_price, confidence, reasoning "
                "FROM predictions ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            pred_line = (
                f"{pred['horizon']} target ${pred['predicted_price']:.2f} "
                f"({pred['confidence']:.0%}) — {pred['reasoning'][:120]}"
                if pred else "no active prediction"
            )

            # Latest options snapshot
            opts = conn.execute(
                "SELECT max_pain_strike, put_call_ratio, net_oi_bias, expiration "
                "FROM options_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            opts_line = (
                f"max-pain ${opts['max_pain_strike']:.2f} for {opts['expiration']}, "
                f"P/C {opts['put_call_ratio']:.2f}, bias {opts['net_oi_bias']}"
                if opts else "no options snapshot"
            )
            conn.close()

            headline_line = (
                f"{top_headline['sentiment_label']}: {top_headline['headline'][:160]}"
                if top_headline else "n/a"
            )
            agent_block = "\n".join(agent_lines) if agent_lines else "  (no recent agent logs)"

            prompt = (
                "You are the SWOT synthesizer. Produce a plain-English SWOT for GME based ONLY on "
                "the verified data below. Do NOT invent dates, filings, insider trades, or price "
                "levels outside the ranges given. If a quadrant has nothing genuine to say, write "
                "one honest line (e.g. 'No specific threats surfaced in last 24h').\n\n"
                "OUTPUT FORMAT (EXACTLY this, keep emoji + labels):\n"
                "💪 STRENGTHS\n"
                "  • [bullet citing a specific data point]\n"
                "  • [bullet]\n"
                "⚠️ WEAKNESSES\n"
                "  • [bullet]\n"
                "  • [bullet]\n"
                "🎯 OPPORTUNITIES\n"
                "  • [bullet — cite a price level INSIDE the 5-day range]\n"
                "  • [bullet]\n"
                "☠️ THREATS\n"
                "  • [bullet]\n"
                "  • [bullet]\n\n"
                f"{fact['prompt_line']}\n\n"
                f"News sentiment (24h): {news_tally}\n"
                f"Top headline: {headline_line}\n"
                f"Latest prediction: {pred_line}\n"
                f"Options: {opts_line}\n\n"
                f"Recent agent outputs (24h):\n{agent_block}\n\n"
                "Rules: no markdown headers, no preamble. Max 2 bullets per quadrant, "
                "each under 140 chars. Cite specific numbers/sources where possible."
            )

            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            r = _req.post(
                f"{ollama_host}/api/generate",
                json={"model": "gemma2:9b", "prompt": prompt, "stream": False,
                      "options": {"num_predict": 700, "temperature": 0.3}},
                timeout=90,
            )
            r.raise_for_status()
            swot = r.json().get("response", "").strip()

            from market_state import enforce_levels
            swot = enforce_levels(swot, fact)

            price = fact.get("price") or 0.0
            pct = fact.get("pct_change") or 0.0
            header = (
                f"<b>📊 GME SWOT</b>  ${price:.2f} ({pct:+.2f}%)\n"
                f"<i>{fact.get('timestamp','')[:19].replace('T',' ')}</i>\n\n"
            )
            _send(header + swot[:3500])
        except Exception as e:
            log.error(f"[tgbot] /swot failed: {e}")
            _send(f"❌ /swot failed: {str(e)[:400]}")

    elif cmd == "/test":
        _send("⏳ Running Telegram handler smoke tests…")
        try:
            import subprocess, sys
            import os as os_module

            bot_dir   = os_module.path.dirname(__file__)
            test_file = os_module.path.join(bot_dir, "tests", "test_telegram_handlers.py")

            result = subprocess.run(
                [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
                cwd=bot_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )

            output = result.stdout + result.stderr

            if "passed" in output or "failed" in output:
                import re
                match  = re.search(r"(\d+) passed", output)
                passed = int(match.group(1)) if match else 0
                match  = re.search(r"(\d+) failed", output)
                failed = int(match.group(1)) if match else 0

                if failed == 0 and passed > 0:
                    _send(
                        f"✅ <b>ALL COMMAND TESTS PASSED</b>\n\n"
                        f"Passed: {passed}\n"
                        f"Failed: 0\n"
                        f"Status: 🟢 HEALTHY\n\n"
                        "<i>Every /command branch in the bot executed without "
                        "raising and produced output. Covers /status, /ticks, "
                        "/agents, /standup, /brief, /update, /trove, /learn, "
                        "/lessons, /frequency, /supportme, /test, /compare, "
                        "/help and the unknown-command fallback.</i>"
                    )
                else:
                    _send(
                        f"⚠️ <b>TEST FAILURES</b>\n\n"
                        f"Passed: {passed}\n"
                        f"Failed: {failed}\n\n"
                        f"Last 20 lines:\n<code>{output[-1000:]}</code>"
                    )
            else:
                _send(f"❌ Test run error:\n<code>{output[-500:]}</code>")

        except subprocess.TimeoutExpired:
            _send("❌ Tests timed out after 60 seconds")
        except Exception as e:
            _send(f"❌ Test runner error: {str(e)[:200]}")
            log.error(f"[tgbot] /test failed: {e}")

    elif cmd == "/help":
        _send(
            "<b>📚 GME Trading Bot — Command Guide</b>\n\n"
            "<b>System Commands:</b>\n"
            "/status — system health, tick count, last agent activity\n"
            "/standup — agent daily standup (signals, win rates, team ROI)\n"
            "/signals [N] — list recent signals with short IDs\n"
            "/executed &lt;id&gt; [note] — log that the team acted on a signal\n"
            "/ignored &lt;id&gt; [reason] — log that the team passed on a signal\n"
            "/missed &lt;id&gt; [note] — log that the team wanted to act but missed it\n"
            "/agents — last run time for each agent\n"
            "/ticks — price data received today\n"
            "/freshness — verify agents are reading current data\n\n"
            "<b>Research & Intel:</b>\n"
            "/brief — today's strategy brief from synthesis agent\n"
            "/swot — SWOT synthesis (strengths/weaknesses/opps/threats)\n"
            "/update — force sync local data to Supabase now\n"
            "/trove [TICKERS] — deep-value Trove Score screen (default watchlist if no tickers)\n\n"
            "<b>🧠 Agent Learning:</b>\n"
            "/learn \"<lesson>\" --why \"<reason>\" — teach agents a rule\n"
            "/lessons [topic] — show lessons agents learned\n\n"
            "<b>Settings:</b>\n"
            "/frequency [low|medium|high] — notification level\n\n"
            "<b>☕ Support:</b>\n"
            "/supportme — buy-me-a-coffee / PayPal link\n\n"
            "<b>🧪 Testing:</b>\n"
            "/test — run Telegram handler smoke tests (~1 sec)\n"
            "/force &lt;agent&gt; — force an agent to run now "
            "(valerie, chatty, newsie, pattern, trendy, futurist, georisk, "
            "synthesis, boss, cto)\n\n"
            "<b>💬 Interactive Chat:</b>\n"
            "Just send any question (no slash) to ask:\n"
            "• Current GME price & analysis\n"
            "• Trading strategies & signals\n"
            "• Market & geopolitical context\n"
            "• Questions about curated research docs\n\n"
            "/compare <question> — Get responses from both Gemma & DeepSeek (compare approaches)\n\n"
            "<i>Responses use curated GameStop research, real-time data, and AI analysis.</i>"
        )

    elif cmd == "/compare" and args:
        _send("🤔 Comparing Gemma & DeepSeek... (takes ~1 minute)")
        question = " ".join(args)
        try:
            context = _build_context()
            system_prompt = """You are the GME trading team's factual intelligence assistant.
You have access to real-time trading data. Answer questions about GME, markets, and geopolitics.
Be factual and honest — tell the truth even if it contradicts bullish sentiment.
Keep responses brief (1 paragraph max). Think: Bloomberg meets a knowledgeable friend."""

            user_message = f"{context}\n\nQuestion: {question}"
            responses = {}

            # Get both models' responses
            for model_name in ["gemma2:9b", "deepseek-r1:8b"]:
                try:
                    r = requests.post("http://localhost:11434/api/generate", json={
                        "model": model_name,
                        "prompt": f"{system_prompt}\n\n{user_message}",
                        "stream": False,
                    }, timeout=60)
                    if r.status_code == 200:
                        responses[model_name] = r.json().get("response", "").strip()
                except Exception as e:
                    responses[model_name] = f"Error: {e}"

            # Format both responses
            if responses.get("gemma2:9b") or responses.get("deepseek-r1:8b"):
                msg = "<b>🤖 Model Comparison</b>\n\n"
                msg += f"<b>Q:</b> {question}\n\n"
                if responses.get("gemma2:9b"):
                    msg += f"<b>Gemma (Fast):</b>\n{responses['gemma2:9b']}\n\n"
                if responses.get("deepseek-r1:8b"):
                    msg += f"<b>DeepSeek (Complex):</b>\n{responses['deepseek-r1:8b']}"
                _send(msg)
            else:
                _send("❌ Both models failed. Check Ollama connection.")
        except Exception as e:
            _send(f"Compare failed: {e}")

    else:
        _send(
            "<b>Available commands:</b>\n"
            "/help — full command guide and chat capabilities\n"
            "/status — system health\n"
            "/standup — agent daily standup (ROI, win rates)\n"
            "/ticks — price data received\n"
            "/freshness — verify data freshness\n"
            "/agents — last agent activity\n"
            "/brief — today's strategy in plain English\n"
            "/swot — SWOT synthesis\n"
            "/update — sync data to Supabase now\n"
            "/trove [TICKERS] — deep-value score screen\n"
            "/test — run Telegram handler smoke tests\n"
            "/supportme — buy-me-a-coffee / PayPal link\n"
            "/frequency — notification settings\n\n"
            "Send /help for detailed guide."
        )


def _build_context() -> str:
    """Gather recent agent context for LLM chat."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Latest price
        price = conn.execute(
            "SELECT close, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        # Latest synthesis
        synthesis = conn.execute(
            "SELECT content FROM agent_logs WHERE task_type='synthesis' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        # Latest prediction
        pred = conn.execute(
            "SELECT predicted_price, confidence FROM predictions ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        # Latest Chatty commentary
        chatty = conn.execute(
            "SELECT content FROM agent_logs WHERE agent_name='Chatty' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        conn.close()

        context = "Recent trading context:\n"
        if price:
            context += f"- Latest price: ${price['close']:.2f} (from {price['timestamp'][:10]})\n"
        if synthesis:
            context += f"- Team consensus: {synthesis['content'][:200]}\n"
        if pred:
            context += f"- Next prediction: ${pred['predicted_price']:.2f} (confidence {pred['confidence']:.0%})\n"
        if chatty:
            context += f"- Latest commentary: {chatty['content'][:150]}\n"

        return context
    except Exception as e:
        log.error(f"[tgbot] context build failed: {e}")
        return ""


def _query_notebook_lm(question: str) -> str | None:
    """Query both Google Notebook LM notebooks and synthesize insights.

    Returns synthesized response if successful, None to fall back to other LLMs.
    Gracefully handles billing errors (402) by falling back.
    """
    try:
        import google.generativeai as genai
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return None

        genai.configure(api_key=api_key)

        # Curated GameStop notebook IDs
        notebooks = {
            "primary": "74adb871-cae3-4a33-8a9b-ae3ec4160d0b",
            "secondary": "5ffcf2d6-7bda-4792-a801-454e44de0f36",
        }

        responses = {}
        for name, nb_id in notebooks.items():
            try:
                response = genai.GenerativeModel("gemini-2.5-flash").generate_content(
                    f"""You are analyzing curated GameStop documents from our {name} collection.
Answer this question based on the provided materials:

Question: {question}

Provide a factual, evidence-based response citing the documents where applicable.
Keep it brief (2-3 sentences max)."""
                )
                if response and response.text:
                    responses[name] = response.text.strip()
            except Exception as e:
                err = str(e)
                if "402" in err or "Insufficient Balance" in err:
                    log.warning(f"[tgbot] Notebook LM ({name}): Google API billing error — skipping to free LLMs")
                    return None
                log.debug(f"[tgbot] Notebook LM ({name}) failed: {e}")

        if not responses:
            return None

        # Synthesize insights from both notebooks
        combined = "\n".join(responses.values())
        synthesis = genai.GenerativeModel("gemini-2.5-flash").generate_content(
            f"""You've gathered insights from two curated GameStop research collections:

Primary collection: {responses.get('primary', '(no response)')}

Secondary collection: {responses.get('secondary', '(no response)')}

Original question: {question}

Synthesize these perspectives into one coherent, evidence-based answer for a trader.
Highlight any conflicts or different angles. Keep it brief for Telegram (1-2 short paragraphs max)."""
        )

        if synthesis and synthesis.text:
            return synthesis.text.strip()
    except Exception as e:
        err = str(e)
        if "402" in err or "Insufficient Balance" in err:
            log.warning(f"[tgbot] Notebook LM synthesis: Google API billing error — skipping to free LLMs")
            return None
        log.debug(f"[tgbot] Notebook LM synthesis failed: {e}")

    return None


def _ask_llm(question: str, context: str) -> str:
    """Ask a question with fallback chain: Notebook LM → Gemma → DeepSeek → Gemini (paid).

    Uses local Ollama models first, falls back to paid APIs only if both local models fail.
    """

    system_prompt = """You are the GME trading team's factual intelligence assistant.
You have access to real-time trading data. Answer questions about GME, markets, and geopolitics.
Be factual and honest — tell the truth even if it contradicts bullish sentiment.
Keep responses brief for Telegram (1-2 short paragraphs max).
Think: Bloomberg terminal meets a knowledgeable friend who reads a lot."""

    user_message = f"{context}\n\nQuestion: {question}"

    # Try Notebook LM first (curated GameStop docs)
    notebook_response = _query_notebook_lm(question)
    if notebook_response:
        log.info("[tgbot] Response from Notebook LM")
        return notebook_response

    # Try local Ollama models: Gemma → DeepSeek
    for model_name in ["gemma2:9b", "deepseek-r1:8b"]:
        try:
            r = requests.post("http://localhost:11434/api/generate", json={
                "model": model_name,
                "prompt": f"{system_prompt}\n\n{user_message}",
                "stream": False,
            }, timeout=30)
            if r.status_code == 200:
                response = r.json().get("response", "").strip()
                log.info(f"[tgbot] Response from {model_name}")
                return response
        except Exception as e:
            log.debug(f"[tgbot] {model_name} failed: {e}")

    # Try Gemini Flash (paid fallback, only if local models fail)
    if os.getenv("GOOGLE_API_KEY"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content(f"{system_prompt}\n\n{user_message}")
            log.info("[tgbot] Response from Gemini Flash")
            return response.text.strip()
        except Exception as e:
            log.debug(f"[tgbot] Gemini failed: {e}")

    return "Sorry, no LLMs available right now. Try again later."


def _handle_chat(text: str):
    """Handle plain-text questions by asking LLM with context."""
    try:
        context = _build_context()
        response = _ask_llm(text, context)
        _send(f"🤖 <i>{response}</i>")
        log.info(f"[tgbot] Chat: {text[:50]}... → response sent")
    except Exception as e:
        log.error(f"[tgbot] chat handler failed: {e}")
        _send("❌ Error processing your question. Try again.")


def _register_commands():
    """Register bot commands with Telegram so they appear in autocomplete."""
    if not ENABLED:
        return
    commands = [
        {"command": "brief",     "description": "Strategy brief — price, direction, agent signals"},
        {"command": "swot",      "description": "SWOT — strengths, weaknesses, opportunities, threats"},
        {"command": "status",    "description": "System heartbeat — ticks, agents, last activity"},
        {"command": "standup",   "description": "Agent scorecard — signals, win rates, OK %"},
        {"command": "signals",   "description": "List recent signals with short IDs"},
        {"command": "executed",  "description": "Log a signal as executed — /executed <id> [note]"},
        {"command": "ignored",   "description": "Log a signal as ignored — /ignored <id> [reason]"},
        {"command": "missed",    "description": "Log a signal as missed — /missed <id> [note]"},
        {"command": "agents",    "description": "Last-run timestamp for every agent"},
        {"command": "freshness", "description": "Are agents reading fresh data? (staleness check)"},
        {"command": "ticks",     "description": "Price ticks received today"},
        {"command": "force",     "description": "Run an agent on demand — /force valerie|newsie|futurist|…"},
        {"command": "compare",   "description": "Gemma vs DeepSeek side-by-side — /compare <question>"},
        {"command": "trove",     "description": "Deep-value Trove screen — /trove [TICKERS]"},
        {"command": "learn",     "description": "Teach agents a rule — /learn \"…\" --why \"…\""},
        {"command": "lessons",   "description": "Show rules agents have learned"},
        {"command": "update",    "description": "Sync local SQLite → Supabase now"},
        {"command": "frequency", "description": "Notification volume — low | medium | high"},
        {"command": "test",      "description": "Run Telegram handler smoke tests (~1 sec)"},
        {"command": "supportme", "description": "Tip jar — buy-me-a-coffee / PayPal"},
        {"command": "help",      "description": "Full command reference + chat tips"},
    ]
    try:
        requests.post(f"{BASE_URL}/setMyCommands", json={"commands": commands}, timeout=10)
        log.info("[tgbot] Commands registered with Telegram")
    except Exception as e:
        log.warning(f"[tgbot] Failed to register commands: {e}")


def run_bot():
    if not ENABLED:
        log.warning("[tgbot] Telegram not configured — bot disabled")
        return

    _ensure_settings_table()
    _register_commands()
    log.info("[tgbot] Two-way Telegram bot started")
    _send("🤖 <b>GME Bot online.</b> Send /status for system health.")

    offset = 0
    while True:
        try:
            updates = _get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != CHAT_ID:
                    continue  # ignore messages from other chats

                if text.startswith("/"):
                    log.info(f"[tgbot] Command: {text}")
                    from_info = msg.get("from") or {}
                    user = (from_info.get("username")
                            or from_info.get("first_name")
                            or "team")
                    handle_command(text, user=user)
                elif text:
                    log.info(f"[tgbot] Chat: {text[:50]}")
                    _handle_chat(text)
        except Exception as e:
            log.error(f"[tgbot] Poll error: {e}")
            time.sleep(5)


def start_bot_thread() -> threading.Thread:
    t = threading.Thread(target=run_bot, daemon=True, name="telegram-bot")
    t.start()
    return t


def should_notify(level: str) -> bool:
    """Check if a notification at the given importance level should be sent.

    level: 'low' (daily), 'medium' (trades), 'high' (agent decisions)
    """
    freq = _get_frequency()
    order = {"low": 0, "medium": 1, "high": 2}
    return order.get(freq, 1) >= order.get(level, 1)
