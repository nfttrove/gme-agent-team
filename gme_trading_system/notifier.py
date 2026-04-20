"""
Telegram Push Notification System

Sends critical trading alerts directly to your phone via Telegram bot.

Setup (5 minutes):
  1. Open Telegram → search @BotFather → send /newbot → name it (e.g. GMEAlertBot)
  2. BotFather gives you a token → add to .env as TELEGRAM_BOT_TOKEN=...
  3. Start your bot (send it /start)
  4. Get your chat ID: message @userinfobot → it replies with your chat ID
     OR visit https://api.telegram.org/bot<TOKEN>/getUpdates after messaging your bot
  5. Add to .env: TELEGRAM_CHAT_ID=<your_chat_id>

Usage:
    from notifier import notify, notify_trade, notify_cto_alert, notify_immunity_red
    notify("Test message")              # plain alert
    notify_trade("BUY", 22.10, 0.73)   # trade approved
    notify_cto_alert("AMC", "restructuring_advisor_hired", 0.99)  # PE signal
    notify_immunity_red("debt_free", "GME issued $500M bonds")    # EMERGENCY
"""
import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns True on success."""
    if not _ENABLED:
        log.info(f"[notify] Telegram not configured. Message would have been:\n{text}")
        return False
    try:
        resp = requests.post(
            f"{_BASE_URL}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log.warning(f"[notify] Telegram error {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as e:
        log.error(f"[notify] Telegram send failed: {e}")
        return False


# ── Alert types ────────────────────────────────────────────────────────────────

def notify(message: str) -> bool:
    """Generic plain-text notification."""
    ts = datetime.now().strftime("%H:%M:%S")
    return _send(f"<b>[GME System]</b> {ts}\n{message}")


def notify_trade(action: str, price: float, confidence: float,
                 stop_loss: float = 0, take_profit: float = 0,
                 quantity: float = 0, status: str = "APPROVED") -> bool:
    """
    Notify when Boss approves or rejects a trade.
    action: BUY | SELL | HOLD
    """
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(action, "📊")
    status_emoji = "✅" if status == "APPROVED" else "❌"
    msg = (
        f"{status_emoji} <b>TRADE {status}</b>\n"
        f"{emoji} {action} GME @ <b>${price:.2f}</b>\n"
        f"Confidence: {confidence:.0%}\n"
    )
    if stop_loss:
        msg += f"SL: ${stop_loss:.2f}  |  TP: ${take_profit:.2f}\n"
    if quantity:
        msg += f"Qty: {quantity} shares\n"
    msg += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}</i>"
    return _send(msg)


def notify_cto_alert(ticker: str, signal_name: str, confidence: float,
                     action: str = "SHORT", timeline_months: int = 0,
                     headline: str = "") -> bool:
    """
    CTO structural signal detected — PE playbook firing on a stock.
    High confidence (>80%) signals always send. Lower confidence only if action=EXIT.
    """
    if confidence < 0.70 and action not in ("SHORT", "EXIT"):
        return False  # suppress low-confidence noise

    urgency = "🚨" if confidence >= 0.90 else ("⚠️" if confidence >= 0.75 else "📌")
    msg = (
        f"{urgency} <b>PE PLAYBOOK SIGNAL</b>\n"
        f"Ticker: <b>{ticker}</b>\n"
        f"Signal: <code>{signal_name}</code>\n"
        f"Confidence: {confidence:.0%}  |  Action: <b>{action}</b>\n"
    )
    if timeline_months:
        msg += f"Timeline: ~{timeline_months} months to event\n"
    if headline:
        msg += f"<i>{headline[:200]}</i>\n"
    msg += f"\n<i>{datetime.now().strftime('%Y-%m-%d %H:%M ET')}</i>"
    return _send(msg)


def notify_immunity_red(check_name: str, detail: str) -> bool:
    """
    EMERGENCY: A GME immunity condition turned RED.
    This is the highest priority alert — means the thesis is compromised.
    """
    msg = (
        f"🆘 <b>GME IMMUNITY ALERT — {check_name.upper()}</b>\n\n"
        f"{detail}\n\n"
        f"⚡ <b>ACTION REQUIRED: Review position immediately.</b>\n"
        f"If PE playbook weapon has been restored, the squeeze thesis changes.\n\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}</i>"
    )
    return _send(msg)


def notify_max_pain(strike: float, current_price: float, friday_date: str,
                    net_oi_direction: str = "") -> bool:
    """Weekly max pain update — sent Monday morning."""
    diff = current_price - strike
    direction = "ABOVE" if diff > 0 else "BELOW"
    emoji = "📌"
    msg = (
        f"{emoji} <b>OPTIONS MAX PAIN — {friday_date}</b>\n\n"
        f"Max Pain Strike: <b>${strike:.2f}</b>\n"
        f"Current Price: ${current_price:.2f} ({direction} by ${abs(diff):.2f})\n"
    )
    if net_oi_direction:
        msg += f"Net OI bias: {net_oi_direction}\n"
    msg += (
        f"\nMM hedging pressure pulls price toward ${strike:.2f} into expiry.\n"
        f"<i>Expiry: {friday_date}</i>"
    )
    return _send(msg)


def notify_social_signal(username: str, tweet_text: str, signal_type: str = "INFO") -> bool:
    """
    Alert when a tracked account (Cohen, Burry, Cheng) posts something relevant.
    signal_type: INFO | BULLISH | BEARISH | CRITICAL
    """
    emoji = {"INFO": "💬", "BULLISH": "🐂", "BEARISH": "🐻", "CRITICAL": "🚨"}.get(signal_type, "💬")
    msg = (
        f"{emoji} <b>@{username} posted</b>\n\n"
        f'"{tweet_text[:400]}"\n\n'
        f"Signal: <b>{signal_type}</b>\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M ET')}</i>"
    )
    return _send(msg)


def notify_watchdog_alert(age_seconds: int) -> bool:
    """TradingView webhook has gone silent — data gap warning."""
    mins = age_seconds // 60
    msg = (
        f"⚠️ <b>DATA FEED ALERT</b>\n\n"
        f"TradingView webhook silent for <b>{mins} minutes</b>.\n"
        f"Check: ngrok running? TradingView alert active? Internet connection?\n\n"
        f"1-second data NOT flowing. Alpaca backup may be filling gaps.\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M ET')}</i>"
    )
    return _send(msg)


def notify_daily_summary(pnl: float, win_rate: float, trades: int,
                         pred_error_pct: float, gme_close: float) -> bool:
    """End-of-day summary push."""
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    msg = (
        f"📊 <b>DAILY SUMMARY</b> — {datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"GME Close: <b>${gme_close:.2f}</b>\n\n"
        f"{pnl_emoji} P&L (paper): <b>${pnl:+.2f}</b>\n"
        f"Win Rate: {win_rate:.0%}  |  Trades: {trades}\n"
        f"Prediction error: ±{pred_error_pct:.2f}%\n\n"
        f"<i>Learner debrief complete. Strategy adapts overnight.</i>"
    )
    return _send(msg)


def notify_periodic_brief(price: float, pct_change: float, consensus: str,
                         top_signal: str, geo_risk: str, prediction: str) -> bool:
    """Send a 4-hour intelligence digest (human-readable)."""
    ts = datetime.now().strftime("%I:%M %p")
    msg = (
        f"📊 <b>GME INTELLIGENCE BRIEF</b> — {ts} ET\n\n"
        f"💰 <b>PRICE</b>: ${price:.2f} ({pct_change:+.1f}%)\n"
        f"🧠 <b>CONSENSUS</b>: {consensus}\n"
        f"🔮 <b>PREDICTION</b>: {prediction}\n"
        f"📰 <b>TOP SIGNAL</b>: {top_signal}\n"
        f"🌍 <b>GEOPOLITICAL RISK</b>: {geo_risk}"
    )
    return _send(msg)


# ── Test ───────────────────────────────────────────────────────────────────────

def test_connection() -> bool:
    """Send a test message to verify Telegram is configured correctly."""
    if not _ENABLED:
        print(
            "\n[notifier] Telegram NOT configured.\n"
            "Add to .env:\n"
            "  TELEGRAM_BOT_TOKEN=your-bot-token\n"
            "  TELEGRAM_CHAT_ID=your-chat-id\n"
            "\nSetup guide: https://core.telegram.org/bots#how-do-i-create-a-bot\n"
        )
        return False
    ok = _send(
        "🤖 <b>GME Trading System</b>\n\n"
        "Telegram notifications are working.\n"
        "You'll receive alerts for:\n"
        "  • Trade approvals/rejections\n"
        "  • PE playbook signals on any stock\n"
        "  • GME immunity status changes\n"
        "  • Weekly max pain updates\n"
        "  • Ryan Cohen / Michael Burry posts\n"
        "  • Data feed alerts\n"
        "  • Daily P&amp;L summary\n\n"
        "<i>System online.</i>"
    )
    if ok:
        print("[notifier] ✅ Telegram test message sent successfully.")
    return ok


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    test_connection()
