"""
Telegram Push Notification System — Chat-Native Burst Format

Sends critical trading alerts directly to your phone via Telegram bot.
Messages are formatted as short bursts (5–8 lines each) for 1-second comprehension
while scrolling, with minimal emoji and aggressive spacing.

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
    notify_trade("BUY", 22.10, 0.73)   # trade approved (single burst)
    notify_cto_alert("AMC", "restructuring_advisor_hired", 0.99)  # PE signal burst
    notify_immunity_red("debt_free", "GME issued $500M bonds")    # EMERGENCY burst
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from circuit_breaker import get_breaker, CircuitOpenError
from message_formatters_v2 import (
    format_header, format_signal_burst, format_market_burst, format_trade_burst,
    format_structure_burst, format_alert_burst, format_impact_burst,
    format_social_burst, format_watchdog_burst, format_summary_burst,
    format_update_burst, format_stale_burst, format_pattern_burst,
    burst_signal_with_market, get_ny_time_short
)
from trading_glossary import add_emoji_definitions

ET = ZoneInfo("America/New_York")

load_dotenv()

log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_OWNER_CHAT_ID = os.getenv("TELEGRAM_OWNER_CHAT_ID", "")

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Per-agent emojis so two LOW-severity alerts from different agents don't
# look identical at a glance. Voice forwarder uses these too (agent_voice.py).
AGENT_EMOJI = {
    "Valerie":   "✅",  # data validation
    "Chatty":    "💬",  # commentary
    "Newsie":    "📰",  # news sentiment
    "Pattern":   "🎯",  # chart patterns (daily)
    "Pattern Intraday": "⚡",  # 5-min chart patterns
    "Trendy":    "📈",  # daily trend
    "Futurist":  "🔮",  # price prediction
    "GeoRisk":   "🌍",  # geopolitical
    "Synthesis": "🧠",  # cross-agent consensus
    "Boss":      "🧭",  # daily mission briefing
    "CTO":       "🛡️",  # structural / Trove score
    "Calibrator":"📐",  # confidence calibration
}

_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message to both public channel and owner DM. Returns True if at least one succeeds."""
    if not _ENABLED:
        log.info(f"[notify] Telegram not configured. Message would have been:\n{text}")
        return False
    breaker = get_breaker("telegram")
    success = False
    # Send to both public channel and owner DM
    chat_ids = [cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_OWNER_CHAT_ID] if cid]
    for chat_id in chat_ids:
        try:
            resp = breaker.call(
                requests.post,
                f"{_BASE_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )
            if resp.status_code == 200:
                success = True
            else:
                log.warning(f"[notify] Telegram error {resp.status_code} to {chat_id}: {resp.text[:200]}")
        except CircuitOpenError:
            log.warning(f"[notify] Telegram circuit open — skipping notification to {chat_id}")
        except requests.RequestException as e:
            log.error(f"[notify] Telegram send failed to {chat_id}: {e}")
    return success


def _send_burst(messages: list[str]) -> bool:
    """Send a list of messages in rapid succession. Returns True if at least one succeeds."""
    if not messages:
        return False
    any_success = False
    for msg in messages:
        if _send(msg):
            any_success = True
    return any_success


def _send_photo(photo_path: Path, caption: str, parse_mode: str = "HTML") -> bool:
    """Send a photo with caption to all configured chat IDs. Returns True if at least one succeeds."""
    if not _ENABLED:
        log.info(f"[notify] Telegram not configured. Photo {photo_path} would have been sent.")
        return False
    if not Path(photo_path).exists():
        log.warning(f"[notify] photo not found at {photo_path}; falling back to text-only")
        return _send(caption, parse_mode=parse_mode)
    breaker = get_breaker("telegram")
    success = False
    chat_ids = [cid for cid in [TELEGRAM_CHAT_ID, TELEGRAM_OWNER_CHAT_ID] if cid]
    for chat_id in chat_ids:
        try:
            with open(photo_path, "rb") as fh:
                resp = breaker.call(
                    requests.post,
                    f"{_BASE_URL}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                    files={"photo": fh},
                    timeout=20,
                )
            if resp.status_code == 200:
                success = True
            else:
                log.warning(f"[notify] Telegram sendPhoto error {resp.status_code} to {chat_id}: {resp.text[:200]}")
        except CircuitOpenError:
            log.warning(f"[notify] Telegram circuit open — skipping photo to {chat_id}")
        except (requests.RequestException, OSError) as e:
            log.error(f"[notify] Telegram sendPhoto failed to {chat_id}: {e}")
    return success


PROMO_IMAGE = Path(__file__).parent / "assets" / "mygmebot_qr.png"
PROMO_TEXT = (
    "🎮 <b>@mygmebot</b> — Real-time $GME signals\n\n"
    "9-agent analysis system: intraday + daily pattern breakouts, trend shifts, "
    "news sentiment, geopolitical risk, price predictions. "
    "Each with a calibrated confidence score.\n\n"
    "👉 https://t.me/mygmebot\n\n"
    "<i>Not financial advice. May contain errors.</i>"
)


def notify_promo() -> bool:
    """Broadcast the @mygmebot promo (mascot/QR image + caption). Falls back to text if image missing."""
    if PROMO_IMAGE.exists():
        return _send_photo(PROMO_IMAGE, PROMO_TEXT)
    log.warning(f"[notify] promo image missing at {PROMO_IMAGE}; sending text-only")
    return _send(PROMO_TEXT)


# ── Alert types ────────────────────────────────────────────────────────────────

def notify(message: str) -> bool:
    """Generic plain-text notification."""
    ts = datetime.now(ET).strftime("%H:%M:%S")
    return _send(f"<b>[GME System]</b> {ts}\n{message}")


def notify_trade(action: str, price: float, confidence: float,
                 stop_loss: float = 0, take_profit: float = 0,
                 quantity: float = 0, status: str = "APPROVED") -> bool:
    """Burst: Trade approval or rejection (single message).

    Returns True if message sent successfully.
    """
    ts = get_ny_time_short()
    msg = format_trade_burst(
        side=action,
        symbol="GME",
        entry=price,
        target=take_profit if take_profit else None,
        stop_loss=stop_loss if stop_loss else None,
        quantity=int(quantity) if quantity else None,
        timestamp_et=ts
    )
    return _send(msg)


def notify_cto_alert(ticker: str, signal_name: str, confidence: float,
                     action: str = "SHORT", timeline_months: int = 0,
                     headline: str = "") -> bool:
    """Burst: PE playbook structural signal (single message).

    Suppresses low-confidence noise (<70%).
    Returns True if message sent successfully.
    """
    if confidence < 0.70 and action not in ("SHORT", "EXIT"):
        log.info(f"[notify] Suppressing low-confidence CTO alert on {ticker}")
        return False

    ts = get_ny_time_short()
    timeline_str = f"~{timeline_months} months" if timeline_months else None
    msg = format_structure_burst(
        ticker=ticker,
        signal_type=signal_name,
        confidence=f"{confidence:.0%}",
        timeline=timeline_str,
        news_snippet=headline[:120] if headline else None,
        timestamp_et=ts
    )
    return _send(msg)


def notify_immunity_red(check_name: str, detail: str) -> bool:
    """Burst: Immunity breach EMERGENCY alert (single message).

    Highest priority — thesis is compromised.
    Returns True if message sent successfully.
    """
    ts = get_ny_time_short()
    msg = format_alert_burst(
        alert_type=check_name.upper(),
        severity="CRITICAL",
        message=detail,
        action="Review position immediately",
        timestamp_et=ts
    )
    return _send(msg)


def notify_max_pain(strike: float, current_price: float, friday_date: str,
                    net_oi_direction: str = "") -> bool:
    """Burst: Weekly options max pain update (single message).

    Returns True if message sent successfully.
    """
    ts = get_ny_time_short()
    msg = format_impact_burst(
        max_pain=strike,
        current_price=current_price,
        expiry_date=friday_date,
        oi_bias=net_oi_direction if net_oi_direction else None,
        timestamp_et=ts
    )
    return _send(msg)


def notify_social_signal(username: str, tweet_text: str, signal_type: str = "INFO") -> bool:
    """Burst: Social intelligence alert (single ultra-compact message).

    signal_type: INFO | CRITICAL
    Returns True if message sent successfully.
    """
    ts = get_ny_time_short()
    msg = format_social_burst(
        handle=username,
        message=tweet_text,
        urgency="CRITICAL" if signal_type == "CRITICAL" else "INFO",
        timestamp_et=ts
    )
    return _send(msg)


def notify_watchdog_alert(age_seconds: int) -> bool:
    """Burst: Data feed watchdog alert (single message).

    Returns True if message sent successfully.
    """
    ts = get_ny_time_short()
    mins = age_seconds // 60
    msg = format_watchdog_burst(
        feed_name="TradingView",
        duration_offline=mins,
        fallback_status="Alpaca backup active",
        timestamp_et=ts
    )
    return _send(msg)


def notify_daily_summary(pnl: float, win_rate: float, trades: int,
                         pred_error_pct: float, gme_close: float) -> bool:
    """Burst: End-of-day summary (single message).

    Returns True if message sent successfully.
    """
    ts = datetime.now(ET).strftime("%H:%M ET")
    learnings = [
        f"Prediction error ±{pred_error_pct:.1f}%",
    ]
    msg = format_summary_burst(
        price=gme_close,
        pnl=pnl,
        pnl_pct=(pnl / gme_close * 100) if gme_close else None,
        win_rate=win_rate,
        trades_count=trades,
        learnings=learnings,
        timestamp_et=ts
    )
    return _send(msg)


def notify_periodic_brief(price: float, pct_change: float, consensus: str,
                         top_signal: str, geo_risk: str, prediction: str,
                         options: str = "") -> bool:
    """Burst: Periodic 4-hour market update (single message).

    Returns True if message sent successfully.
    """
    ts = get_ny_time_short()
    # Parse consensus direction if it's like "BULLISH 65%"
    consensus_dir = consensus.split()[0].upper() if consensus else "NEUTRAL"
    consensus_pct = int(consensus.split()[1].rstrip("%")) if len(consensus.split()) > 1 else 50

    msg = format_update_burst(
        consensus_direction=consensus_dir,
        consensus_pct=consensus_pct,
        top_signal=top_signal,
        structure_status=prediction,
        risk_flag=geo_risk,
        timestamp_et=ts
    )
    return _send(msg)


def _stale_data_reasons() -> list[str]:
    """Return human-readable reasons the signal layer is reading stale data.
    Empty list means data is fresh. Fails open (returns []) on any error so a
    broken checker can never silently suppress legit signals."""
    try:
        import data_freshness
        return [f"{name}: {detail}" for name, ok, detail in data_freshness.check() if not ok]
    except Exception as e:
        log.warning(f"[notify] freshness check errored, failing open: {e}")
        return []


def notify_signal_alert(agent_name: str, signal_type: str, confidence: float,
                        entry_price: float = None, stop_loss: float = None,
                        take_profit: float = None, reasoning: str = "",
                        alert_id: str = "") -> bool:
    """Burst: Primary signal alert (may send 1 or 2 messages depending on data freshness).

    If data is stale, sends a STALE warning instead.
    Otherwise sends SIGNAL burst (and optionally MARKET context).

    Confidence is shown only (no calibration math exposed).

    Usage:
        notify_signal_alert(
            agent_name="Futurist",
            signal_type="price_prediction",
            confidence=0.78,
            entry_price=23.45,
            stop_loss=22.80,
            take_profit=25.50,
            reasoning="RSI oversold + volume spike",
            alert_id="abc123"
        )
    """
    stale = _stale_data_reasons()
    if stale:
        log.warning(f"[notify] Suppressing {agent_name} signal — stale data: {stale}")
        ts = get_ny_time_short()
        msg = format_stale_burst(
            stale_data_type=", ".join([s.split(":")[0] for s in stale[:3]]),
            time_gap=None,
            impact="Signals suppressed until fresh data",
            timestamp_et=ts
        )
        return _send(msg)

    ts = get_ny_time_short()
    # Infer direction from signal_type or entry vs target
    direction = "BULLISH" if take_profit and entry_price and take_profit > entry_price else "BEARISH"

    # Parse reasons from reasoning string. Orchestrator commonly builds
    # reasoning as pipe-separated metadata-style strings (e.g.
    # "DOWN trend | S=$21.98 | R=$25.25 — Price below VWAP, EMA21"). Split on
    # the most "structural" separator first, then clean each part: drop
    # metadata-like fragments (S=$X, R=$X, "X trend", "Consensus: X") since
    # those duplicate fields already shown elsewhere in the burst.
    import re as _re
    reasons = []
    if reasoning:
        # Pick the separator that yields the most parts (>=2)
        candidate_seps = ["•", "\n", "|", ";", " — ", ", "]
        parts = [reasoning]
        for sep in candidate_seps:
            if sep in reasoning:
                split_attempt = [r.strip() for r in reasoning.split(sep) if r.strip()]
                if len(split_attempt) >= 2:
                    parts = split_attempt
                    break

        # Strip metadata-like fragments that duplicate fields shown in the
        # header (direction, target). These leak from orchestrator's
        # pipe-separated reasoning construction.
        _drop_patterns = [
            _re.compile(r"^(BULLISH|BEARISH|NEUTRAL|UP|DOWN|SIDEWAYS)\s+trend\s*$", _re.IGNORECASE),
            _re.compile(r"^S\s*=\s*\$?[\d.]+\s*$", _re.IGNORECASE),
            _re.compile(r"^R\s*=\s*\$?[\d.]+\s*$", _re.IGNORECASE),
            _re.compile(r"^Consensus:\s*\w+\s*$", _re.IGNORECASE),
            _re.compile(r"^Sentiment:\s*[+-]?[\d.]+\s*$", _re.IGNORECASE),
            _re.compile(r"^Trend:\s*\w+\s*$", _re.IGNORECASE),
        ]
        cleaned = []
        for p in parts:
            # Strip leading metadata tokens within a part: "S=$X.XX, Y" → "Y"
            p = _re.sub(r"^S\s*=\s*\$?[\d.]+\s*,?\s*", "", p, flags=_re.IGNORECASE)
            p = _re.sub(r"^R\s*=\s*\$?[\d.]+\s*,?\s*", "", p, flags=_re.IGNORECASE)
            p = p.strip(" .,—-")
            if not p or any(pat.match(p) for pat in _drop_patterns):
                continue
            cleaned.append(p)

        reasons = cleaned[:3]
        # If everything was metadata (e.g. Synthesis's "Consensus: X | Sentiment: Y |
        # Trend: Z" — all redundant with the header), emit the burst with no
        # bullets rather than echoing the metadata back as one ugly line.

    # Format as single SIGNAL burst message — use the agent's own emoji as identity
    msg = format_signal_burst(
        direction=direction,
        target=take_profit,
        confidence=f"{confidence:.0%}",
        reasons=reasons,
        timestamp_et=ts,
        emoji=AGENT_EMOJI.get(agent_name, "🔮"),
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
    ts = get_ny_time_short()
    ok = _send(
        f"🤖 <b>GME System Online</b> | {ts}\n\n"
        "Burst-format Telegram alerts:\n"
        "  • Trade approvals (TRADE burst)\n"
        "  • PE playbook signals (STRUCTURE burst)\n"
        "  • Immunity alerts (ALERT burst)\n"
        "  • Max pain updates (IMPACT burst)\n"
        "  • Social posts (SOCIAL burst)\n"
        "  • Data feed status (WATCHDOG burst)\n"
        "  • Daily summary (CLOSE burst)\n\n"
        "<i>1-second comprehension. No calibration metadata.</i>"
    )
    if ok:
        print("[notifier] ✅ Telegram test message sent successfully.")
    return ok


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    test_connection()
