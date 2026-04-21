"""
Two-way Telegram Bot — command interface for the GME trading system.

Commands:
  /help        — full command guide and chat capabilities
  /status      — system health, agents, tick count
  /balance     — live IBKR account balance
  /ticks       — price ticks received today
  /agents      — last run time for each agent
  /brief       — today's strategy in plain English
  /update      — sync local data to Supabase immediately
  /halt        — pause all new trades (risk override)
  /resume      — re-enable trading
  /frequency   — show current notification frequency
  /frequency low|medium|high — set notification level
               low    = daily summary only
               medium = trades + daily summary (default)
               high   = every agent decision + trades + summary

Interactive chat: send plain text questions for LLM responses with trading context.
Queries dual curated GameStop research collections via Google Notebook LM first,
then falls back to Gemma/Gemini/DeepSeek.

Run as a thread from orchestrator.py.
"""
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, date

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH  = os.path.join(os.path.dirname(__file__), "agent_memory.db")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

ENABLED = bool(TOKEN and CHAT_ID)

_halt_flag = threading.Event()   # set = halted, clear = trading allowed
_HALT_FILE = os.path.join(os.path.dirname(__file__), "halt.flag")


def is_halted() -> bool:
    return _halt_flag.is_set() or os.path.exists(_HALT_FILE)


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


def handle_command(text: str):
    cmd = text.strip().lower().split()[0]
    args = text.strip().split()[1:] if len(text.strip().split()) > 1 else []

    if cmd == "/update":
        _send("⏳ Syncing to Supabase...")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from supabase_sync import _get_client, _load_state, sync_once
            client = _get_client()
            state = _load_state()
            state = sync_once(client, state)
            _send("✅ <b>Supabase sync complete.</b>\nAll local data is now synchronized.")
            log.info("[tgbot] Manual sync triggered")
        except Exception as e:
            _send(f"❌ Sync failed: {e}")
            log.error(f"[tgbot] Sync command failed: {e}")

    elif cmd == "/status":
        tick_count = _db_scalar("SELECT COUNT(*) FROM price_ticks WHERE date(timestamp)=date('now')")
        last_log   = _db_scalar("SELECT agent_name || ': ' || task_type FROM agent_logs ORDER BY timestamp DESC LIMIT 1")
        halt_str   = "HALTED" if is_halted() else "ACTIVE"
        freq       = _get_frequency()
        _send(
            f"<b>GME System Status</b>\n"
            f"Trading: <b>{halt_str}</b>\n"
            f"Ticks today: {tick_count or 0}\n"
            f"Notifications: {freq}\n"
            f"Last agent: {last_log or 'none yet'}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )

    elif cmd == "/balance":
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from broker import get_broker
            broker = get_broker()
            acct = broker.account_summary()
            if "error" in acct:
                _send(f"Balance error: {acct['error']}")
            else:
                _send(
                    f"<b>IBKR Balance ({acct['mode']})</b>\n"
                    f"Equity: ${acct['equity_usd']} (£{acct['equity_gbp']})\n"
                    f"Cash: ${acct['cash_usd']}\n"
                    f"Buying power: ${acct['buying_power_usd']}\n"
                    f"Unrealised P&L: ${acct['unrealized_pnl']}\n"
                    f"Realised today: ${acct['realized_pnl_today']}"
                )
        except Exception as e:
            _send(f"Balance fetch failed: {e}")

    elif cmd == "/ticks":
        today = _db_scalar("SELECT COUNT(*) FROM price_ticks WHERE date(timestamp)=date('now')")
        total = _db_scalar("SELECT COUNT(*) FROM price_ticks")
        latest_price = _db_scalar("SELECT close FROM price_ticks ORDER BY timestamp DESC LIMIT 1")
        _send(
            f"<b>GME Tick Data</b>\n"
            f"Ticks today: {today or 0}\n"
            f"Total in DB: {total or 0}\n"
            f"Latest price: ${latest_price or 'n/a'}"
        )

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

    elif cmd == "/halt":
        _halt_flag.set()
        open(_HALT_FILE, "w").close()
        _send("🛑 <b>Trading HALTED.</b> No new orders will be placed.\nSend /resume to re-enable.")
        log.warning("[tgbot] Trading halted by Telegram command")

    elif cmd == "/resume":
        _halt_flag.clear()
        if os.path.exists(_HALT_FILE):
            os.remove(_HALT_FILE)
        _send("✅ <b>Trading RESUMED.</b> System is active.")
        log.info("[tgbot] Trading resumed by Telegram command")

    elif cmd == "/brief":
        _send("⏳ Generating strategy brief — takes ~30 seconds...")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from crewai import Crew, Process
            from agents import briefing_agent
            from tasks import daily_briefing_task
            crew = Crew(agents=[briefing_agent], tasks=[daily_briefing_task],
                        process=Process.sequential, verbose=False)
            result = crew.kickoff()
            _send(f"<b>📋 STRATEGY BRIEF</b>\n\n{str(result)[:3000]}")
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

    elif cmd == "/help":
        _send(
            "<b>📚 GME Trading Bot — Command Guide</b>\n\n"
            "<b>System Commands:</b>\n"
            "/status — system health, tick count, last agent activity\n"
            "/agents — last run time for each agent\n"
            "/ticks — price data received today\n"
            "/balance — live IBKR account balance\n\n"
            "<b>Research & Intel:</b>\n"
            "/brief — today's strategy brief from synthesis agent\n"
            "/update — force sync local data to Supabase now\n\n"
            "<b>Settings:</b>\n"
            "/frequency [low|medium|high] — notification level\n"
            "/halt — pause trading (risk override)\n"
            "/resume — re-enable trading\n\n"
            "<b>💬 Interactive Chat:</b>\n"
            "Just send any question (no slash) to ask:\n"
            "• Current GME price & analysis\n"
            "• Trading strategies & signals\n"
            "• Market & geopolitical context\n"
            "• Questions about curated research docs\n\n"
            "<i>Responses use curated GameStop research, real-time data, and AI analysis.</i>"
        )

    else:
        _send(
            "<b>Available commands:</b>\n"
            "/help — full command guide and chat capabilities\n"
            "/status — system health\n"
            "/balance — IBKR account balance\n"
            "/ticks — price data received\n"
            "/agents — last agent activity\n"
            "/brief — today's strategy in plain English\n"
            "/update — sync data to Supabase now\n"
            "/halt — pause trading\n"
            "/resume — re-enable trading\n"
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
    """Ask a question to LLM with fallback chain: Notebook LM → Gemma (local) → DeepSeek."""

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

    # Try Gemma 2 9b via Ollama (free, local)
    try:
        r = requests.post("http://localhost:11434/api/generate", json={
            "model": "gemma2:9b",
            "prompt": f"{system_prompt}\n\n{user_message}",
            "stream": False,
        }, timeout=30)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
    except Exception as e:
        log.debug(f"[tgbot] Gemma failed: {e}")

    # Try DeepSeek (cheapest cloud option)
    try:
        r = requests.post("https://api.deepseek.com/v1/chat/completions", json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.3,
            "max_tokens": 500,
        }, headers={
            "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}",
        }, timeout=30)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.debug(f"[tgbot] DeepSeek failed: {e}")

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


def run_bot():
    if not ENABLED:
        log.warning("[tgbot] Telegram not configured — bot disabled")
        return

    _ensure_settings_table()
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
                    handle_command(text)
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
