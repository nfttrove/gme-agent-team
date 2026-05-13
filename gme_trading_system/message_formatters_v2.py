"""Telegram message formatters for chat-native bursts.

Rewrite of message_formatters.py for Telegram-specific dynamics:
- One message = one idea (no hybrid signal+explanation blocks)
- Short bursts (5–8 lines max)
- Minimal emoji (3–4 per message: header, status, direction only)
- Aggressive spacing for rhythm
- NYSE timestamps in header (e.g., "🔮 NEXT | 14:30 ET")
- Confidence shown only (no calibration math)

All functions return a string (single message) or list[str] (multi-message bursts).
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def escape_html(s: str) -> str:
    """Escape <, >, & for Telegram parse_mode=HTML."""
    return html.escape(s or "", quote=False)


def get_ny_time_short() -> str:
    """Return current NYSE time as 'HH:MM ET'."""
    ny_tz = ZoneInfo("America/New_York")
    return datetime.now(ny_tz).strftime("%H:%M ET")


def format_header(emoji: str, message_type: str, timestamp_et: str | None = None) -> str:
    """Format message header with emoji, type, and optional timestamp.

    Args:
        emoji: Single emoji character (e.g., '🔮')
        message_type: Message type name (e.g., 'NEXT', 'MARKET')
        timestamp_et: Optional timestamp in format "HH:MM ET"

    Returns:
        Formatted header string (e.g., "🔮 NEXT | 14:30 ET")
    """
    if timestamp_et:
        return f"{emoji} {message_type} | {timestamp_et}"
    return f"{emoji} {message_type}"


def apply_spacing(lines: list[str], section_breaks: list[int] | None = None) -> str:
    """Join lines with spacing for rhythm.

    Args:
        lines: List of text lines
        section_breaks: Indices after which to insert blank line (e.g., [2, 5])

    Returns:
        Formatted text with aggressive spacing
    """
    if not lines:
        return ""
    if not section_breaks:
        return "\n".join(lines)

    result = []
    for i, line in enumerate(lines):
        result.append(line)
        if i in section_breaks:
            result.append("")
    return "\n".join(result)


def clamp_reasons(reasons: list[str], max_items: int = 3) -> list[str]:
    """Trim bullet list to maximum items.

    Args:
        reasons: List of reason strings
        max_items: Maximum bullets to include (default 3)

    Returns:
        Trimmed list
    """
    if not reasons:
        return []
    return reasons[:max_items]


def strip_calibration(confidence_text: str) -> str:
    """Remove calibration math from confidence text.

    Converts:
        "78% (calibrated from 80%, hit rate 75% on 12 samples)" → "78%"
        "78%" → "78%"

    Args:
        confidence_text: Raw confidence string

    Returns:
        Confidence percentage only
    """
    if not confidence_text:
        return ""
    # Extract just the leading percentage
    match = re.match(r"(\d+%)", confidence_text)
    if match:
        return match.group(1)
    return confidence_text


def minimal_emoji_for_type(message_type: str) -> tuple[str, str | None, str | None]:
    """Return minimal emoji set for a message type.

    Returns:
        (header_emoji, status_emoji, direction_emoji)
    """
    emoji_map = {
        "SIGNAL": ("🧠", None, None),      # Add status/direction inline
        "NEXT": ("🔮", None, None),
        "TREND": ("📈", None, None),       # Direction becomes part of header
        "PATTERN": ("🎯", None, None),
        "MARKET": ("💬", None, None),
        "PRICE": ("💬", None, None),
        "TRADE": ("✅", None, None),
        "IMPACT": ("📊", None, None),
        "STRUCTURE": ("🛡️", None, None),
        "SOCIAL": ("💬", None, None),
        "CLOSE": ("📊", None, None),
        "UPDATE": ("📰", None, None),
        "ALERT": ("🆘", None, None),
        "STALE": ("⚠️", None, None),
        "WATCHDOG": ("⚠️", None, None),
        "RISK": ("🌍", None, None),
    }
    return emoji_map.get(message_type, ("📌", None, None))


# ============================================================================
# BURST FORMATTERS — One per message type
# ============================================================================

def format_signal_burst(
    direction: str,
    target: float | None = None,
    confidence: str | None = None,
    reasons: list[str] | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format a signal recommendation burst.

    Returns ~4–5 line message with direction, target, confidence, and 3 bullet reasons.

    Example:
        🧠 SIGNAL | 14:30 ET

        🟢 Bullish
        Target: $25.50
        Confidence: 78%

        • RSI oversold
        • Volume spike
        • Support hold
    """
    lines = [format_header("🧠", "SIGNAL", timestamp_et), ""]

    # Direction with emoji
    dir_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪", "HOLD": "🟡", "WAIT": "⏳"}.get(
        direction.upper(), "⚪"
    )
    lines.append(f"{dir_emoji} {direction}")

    # Target
    if target:
        lines.append(f"Target: ${target:.2f}")

    # Confidence
    if confidence:
        clean_conf = strip_calibration(confidence)
        lines.append(f"Confidence: {clean_conf}")

    # Reasons
    if reasons:
        clamped = clamp_reasons(reasons, 3)
        lines.append("")
        lines.extend(f"• {r}" for r in clamped)

    return "\n".join(lines)


def format_market_burst(
    price: float | None = None,
    price_change_pct: float | None = None,
    price_range: tuple[float, float] | None = None,
    volume_context: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format market context burst.

    Returns ~4–5 line compact price update.

    Example:
        💬 MARKET | 14:30 ET

        $23.45 ↔️ Sideways
        Range: $21.99–$25.50
        Volume: 1.8x 20d ADV
    """
    lines = [format_header("💬", "MARKET", timestamp_et), ""]

    # Price with direction
    if price is not None:
        direction_emoji = "📈"
        if price_change_pct is not None:
            if price_change_pct < -0.5:
                direction_emoji = "📉"
            elif abs(price_change_pct) <= 0.5:
                direction_emoji = "↔️"
        lines.append(f"${price:.2f} {direction_emoji} " + (
            f"{price_change_pct:+.2f}%" if price_change_pct is not None else ""
        ))

    # Range
    if price_range:
        low, high = price_range
        lines.append(f"Range: ${low:.2f}–${high:.2f}")

    # Volume
    if volume_context:
        lines.append(f"Volume: {volume_context}")

    return "\n".join(lines)


def format_trend_burst(
    direction: str,
    key_level: float | None = None,
    strength: float | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format daily trend burst.

    Returns ~3 line trend snapshot.

    Example:
        📈 TREND | 14:30 ET

        📈 Up | Strength: 65%
        Key level: $24.50
    """
    dir_emoji_map = {"UP": "📈", "DOWN": "📉", "SIDEWAYS": "↔️"}
    dir_emoji = dir_emoji_map.get(direction.upper(), "↔️")

    lines = [format_header("📈", "TREND", timestamp_et), ""]

    # Direction + strength
    if strength is not None:
        strength_pct = int(strength * 100) if strength < 1 else int(strength)
        lines.append(f"{dir_emoji} {direction} | Strength: {strength_pct}%")
    else:
        lines.append(f"{dir_emoji} {direction}")

    # Key level
    if key_level is not None:
        lines.append(f"Key level: ${key_level:.2f}")

    return "\n".join(lines)


def format_price_burst(
    price: float,
    direction: str = "NEUTRAL",
    change_pct: float | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format live price tick burst.

    Returns ~2–3 line ultra-compact price update.

    Example:
        💬 PRICE | 14:30 ET

        $23.45 📉 -1.40%
    """
    lines = [format_header("💬", "PRICE", timestamp_et), ""]

    dir_emoji_map = {"UP": "📈", "DOWN": "📉", "SIDEWAYS": "↔️", "NEUTRAL": "↔️"}
    dir_emoji = dir_emoji_map.get(direction.upper(), "↔️")

    price_line = f"${price:.2f} {dir_emoji}"
    if change_pct is not None:
        price_line += f" {change_pct:+.2f}%"
    lines.append(price_line)

    return "\n".join(lines)


def format_trade_burst(
    side: str,
    symbol: str = "GME",
    entry: float | None = None,
    target: float | None = None,
    stop_loss: float | None = None,
    quantity: int | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format trade approval burst.

    Returns ~6 line trade execution snapshot.

    Example:
        ✅ TRADE | 14:30 ET

        🟢 BUY GME
        Entry: $23.45
        Target: $25.50
        Stop: $22.80
    """
    side_emoji = "🟢" if side.upper() == "BUY" else "🔴"

    lines = [format_header("✅", "TRADE", timestamp_et), ""]
    lines.append(f"{side_emoji} {side.upper()} {symbol}")

    if entry:
        lines.append(f"Entry: ${entry:.2f}")
    if target:
        lines.append(f"Target: ${target:.2f}")
    if stop_loss:
        lines.append(f"Stop: ${stop_loss:.2f}")
    if quantity:
        lines.append(f"Qty: {quantity}")

    return "\n".join(lines)


def format_alert_burst(
    alert_type: str,
    severity: str = "INFO",  # INFO, WARNING, CRITICAL
    message: str = "",
    action: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format alert/warning burst.

    Returns ~4–5 line alert with action if needed.

    Example:
        🆘 ALERT | 14:30 ET

        Immunity breach: DEBT_FREE
        Impact: Dilution risk, squeeze thesis changes

        Action: Review position immediately
    """
    severity_emoji = {"CRITICAL": "🆘", "WARNING": "⚠️", "INFO": "📌"}.get(severity, "📌")
    msg_type = "ALERT" if severity == "CRITICAL" else "WARNING" if severity == "WARNING" else "INFO"

    lines = [format_header(severity_emoji, msg_type, timestamp_et), ""]

    if alert_type:
        lines.append(f"{alert_type}")

    if message:
        lines.append(escape_html(message))

    if action:
        lines.append("")
        lines.append(f"Action: {action}")

    return "\n".join(lines)


def format_summary_burst(
    price: float | None = None,
    pnl: float | None = None,
    pnl_pct: float | None = None,
    win_rate: float | None = None,
    trades_count: int | None = None,
    learnings: list[str] | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format daily close summary burst.

    Returns ~5–6 line end-of-day snapshot.

    Example:
        📊 CLOSE | 16:00 ET

        Close: $23.45
        P&L: +$245.60 (+2.1%)
        Win rate: 72% | Trades: 18

        • Strong support hold
        • Afternoon shorting pressure
    """
    lines = [format_header("📊", "CLOSE", timestamp_et), ""]

    if price is not None:
        lines.append(f"Close: ${price:.2f}")

    if pnl is not None:
        pnl_str = f"+${abs(pnl):.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        if pnl_pct is not None:
            pnl_str += f" ({pnl_pct:+.1f}%)"
        lines.append(f"P&L: {pnl_str}")

    if win_rate is not None and trades_count is not None:
        win_rate_pct = int(win_rate * 100) if win_rate < 1 else int(win_rate)
        lines.append(f"Win rate: {win_rate_pct}% | Trades: {trades_count}")

    if learnings and clamp_reasons(learnings, 2):
        lines.append("")
        lines.extend(f"• {l}" for l in clamp_reasons(learnings, 2))

    return "\n".join(lines)


def format_update_burst(
    consensus_direction: str | None = None,
    consensus_pct: int | None = None,
    top_signal: str | None = None,
    structure_status: str | None = None,
    risk_flag: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format periodic (4-hour) market update burst.

    Returns ~5–6 line status digest.

    Example:
        📰 UPDATE | 14:30 ET

        Consensus: 🟢 Bullish (67%)
        Signal: Pattern breakout on daily
        Structure: No red flags
        Risk: Geopolitical neutral
    """
    lines = [format_header("📰", "UPDATE", timestamp_et), ""]

    if consensus_direction and consensus_pct:
        dir_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(consensus_direction, "⚪")
        lines.append(f"Consensus: {dir_emoji} {consensus_direction} ({consensus_pct}%)")

    if top_signal:
        lines.append(f"Signal: {top_signal}")

    if structure_status:
        status_icon = "✅" if "no" in structure_status.lower() or "clear" in structure_status.lower() else "⚠️"
        lines.append(f"Structure: {status_icon} {structure_status}")

    if risk_flag:
        risk_icon = "🟢" if "neutral" in risk_flag.lower() or "none" in risk_flag.lower() else "🔴"
        lines.append(f"Risk: {risk_icon} {risk_flag}")

    return "\n".join(lines)


def format_pattern_burst(
    timeframe: str,
    pattern_name: str,
    direction: str,
    target: float | None = None,
    confidence: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format pattern alert burst.

    Returns ~5 line pattern recognition message.

    Example:
        🎯 PATTERN | 14:30 ET

        Daily | Double Bottom
        🟢 BULLISH
        Target: $25.50
        Confidence: 85%
    """
    lines = [format_header("🎯", "PATTERN", timestamp_et), ""]

    lines.append(f"{timeframe} | {pattern_name}")

    if direction:
        dir_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(direction.upper(), "⚪")
        lines.append(f"{dir_emoji} {direction}")

    if target:
        lines.append(f"Target: ${target:.2f}")

    if confidence:
        clean_conf = strip_calibration(confidence)
        lines.append(f"Confidence: {clean_conf}")

    return "\n".join(lines)


def format_impact_burst(
    max_pain: float,
    current_price: float,
    expiry_date: str | None = None,
    oi_bias: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format options max pain burst.

    Returns ~4–5 line options impact message.

    Example:
        📊 IMPACT | 14:30 ET

        Max Pain: $24.50
        Current: $23.20 (Below by $1.30)
        OI Bias: Slightly bullish
        Expiry: 2026-05-17
    """
    lines = [format_header("📊", "IMPACT", timestamp_et), ""]

    lines.append(f"Max Pain: ${max_pain:.2f}")

    diff = current_price - max_pain
    diff_icon = "Below" if diff < 0 else "Above"
    lines.append(f"Current: ${current_price:.2f} ({diff_icon} by ${abs(diff):.2f})")

    if oi_bias:
        lines.append(f"OI Bias: {oi_bias}")

    if expiry_date:
        lines.append(f"Expiry: {expiry_date}")

    return "\n".join(lines)


def format_structure_burst(
    ticker: str,
    signal_type: str,
    confidence: str | None = None,
    timeline: str | None = None,
    news_snippet: str | None = None,
    bullets: list[str] | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format PE playbook / CTO structural signal (Bloomberg-terminal style).

    Returns ~5–6 line structural pattern message. Ticker shown in header line
    when not GME (the primary focus). For GME, the headline is the signal itself.

    Example (non-GME, e.g. AMC PE playbook):
        🛡️ STRUCTURE | 14:30 ET

        AMC | restructuring_advisor_hired
        Confidence: 95% | Timeline: ~6 months

    Example (GME, Trove score with bullets):
        🛡️ CTO | 14:30 ET

        Structural Score: 7.2/10
        Long-term thesis intact

        • Cohen filings unchanged
        • Cash: $4.6B
    """
    lines = [format_header("🛡️", "STRUCTURE", timestamp_et), ""]

    # Headline — ticker + signal compressed if non-GME; just signal if GME
    if ticker and ticker.upper() != "GME":
        lines.append(f"{ticker} | {signal_type}")
    else:
        lines.append(signal_type)

    if timeline:
        lines.append(timeline)

    # Confidence + timeline compressed onto one line when both present
    if confidence and not timeline:
        clean_conf = strip_calibration(confidence)
        lines.append(f"Confidence: {clean_conf}")
    elif confidence and timeline:
        # Replace timeline-only line with combined form
        clean_conf = strip_calibration(confidence)
        lines[-1] = f"Confidence: {clean_conf} | Timeline: {timeline}"

    # Bullets win over news snippet (preferred for GME Trove score)
    if bullets:
        lines.append("")
        lines.extend(f"• {b}" for b in clamp_reasons(bullets, 4))
    elif news_snippet:
        lines.append(f"News: {escape_html(news_snippet[:80])}")

    return "\n".join(lines)


def format_social_burst(
    handle: str,
    message: str | None = None,
    urgency: str = "INFO",
    timestamp_et: str | None = None,
) -> str:
    """Format social intelligence burst.

    Returns ~2–3 line social signal (ultra-compact).

    Example:
        💬 SOCIAL | 14:30 ET

        @RoaringKitty
        "New position update in latest filing..."
    """
    severity_emoji = {"CRITICAL": "🚨", "WARNING": "⚠️", "INFO": "💬"}.get(urgency, "💬")

    lines = [format_header(severity_emoji, "SOCIAL", timestamp_et), ""]
    lines.append(f"@{handle}")

    if message:
        # Compact quote snippet
        snippet = message[:120]
        lines.append(f'"{escape_html(snippet)}"')

    return "\n".join(lines)


def format_risk_burst(
    risk_type: str,
    assessment: str,
    impact_on_gme: str | None = None,
    severity: str = "moderate",  # low, moderate, high
    timestamp_et: str | None = None,
) -> str:
    """Format geopolitical/macro risk burst (compressed).

    Returns ~3–4 line risk alert with sharper language.

    Example:
        🌍 RISK | 14:30 ET

        Tariff escalation
        ⚠️ Moderate China-US tension
        Impact: Retail pressure
    """
    lines = [format_header("🌍", "RISK", timestamp_et), ""]

    lines.append(risk_type)

    severity_emoji = {"low": "🟢", "moderate": "⚠️", "high": "🔴"}.get(severity.lower(), "⚠️")
    lines.append(f"{severity_emoji} {assessment}")

    if impact_on_gme:
        lines.append(f"Impact: {impact_on_gme}")

    return "\n".join(lines)


def format_stale_burst(
    stale_data_type: str,
    time_gap: int | None = None,  # minutes
    impact: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format data freshness warning burst.

    Returns ~3–4 line stale data alert.

    Example:
        ⚠️ STALE | 14:30 ET

        Data: price_ticks
        Age: 8 minutes
        Impact: Signals suppressed until fresh
    """
    lines = [format_header("⚠️", "STALE", timestamp_et), ""]

    lines.append(f"Data: {stale_data_type}")

    if time_gap is not None:
        lines.append(f"Age: {time_gap} minute{'s' if time_gap != 1 else ''}")

    if impact:
        lines.append(f"Impact: {impact}")

    return "\n".join(lines)


def format_watchdog_burst(
    feed_name: str,
    duration_offline: int,  # minutes
    fallback_status: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format data feed watchdog alert burst.

    Returns ~3–4 line feed status message.

    Example:
        ⚠️ WATCHDOG | 14:30 ET

        Feed: TradingView
        Offline: 15 minutes
        Fallback: Alpaca backup active
    """
    lines = [format_header("⚠️", "WATCHDOG", timestamp_et), ""]

    lines.append(f"Feed: {feed_name}")
    lines.append(f"Offline: {duration_offline} minute{'s' if duration_offline != 1 else ''}")

    if fallback_status:
        lines.append(f"Fallback: {fallback_status}")

    return "\n".join(lines)


def format_boss_burst(
    bias: str,
    bias_pct: int | None = None,
    action: str | None = None,
    watch_level: str | None = None,
    risk_flag: str | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format Boss daily orders burst (mission/orders style).

    Distinct from generic UPDATE — uses imperative orders language.

    Example:
        🧭 BOSS | 09:35 ET

        📋 Today's Orders:
        • Bias: 🔴 BEARISH (67%)
        • Action: WAIT
        • Watch: $22.50 breakout
        • Risk: Geopolitical moderate
    """
    lines = [format_header("🧭", "BOSS", timestamp_et), ""]
    lines.append("📋 Today's Orders:")

    if bias:
        bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(bias.upper(), "⚪")
        bias_line = f"• Bias: {bias_emoji} {bias}"
        if bias_pct is not None:
            bias_line += f" ({bias_pct}%)"
        lines.append(bias_line)

    if action:
        lines.append(f"• Action: {action}")

    if watch_level:
        lines.append(f"• Watch: {watch_level}")

    if risk_flag:
        lines.append(f"• Risk: {risk_flag}")

    return "\n".join(lines)


def format_calibrator_burst(
    weight_updates: list[tuple[str, float, str]] | None = None,
    timestamp_et: str | None = None,
) -> str:
    """Format Calibrator nightly weight update burst.

    Per-agent multiplier list with direction arrows.

    Args:
        weight_updates: List of (agent_name, multiplier, direction) tuples
                        direction: "up" | "down" | "flat"

    Example:
        📐 CALIBRATOR | 20:00 ET

        Nightly weight updates:
        • Futurist: 0.95x ↓
        • Pattern: 1.10x ↑
        • Newsie: 0.85x ↓
        • Trendy: 1.00x →
    """
    lines = [format_header("📐", "CALIBRATOR", timestamp_et), ""]
    lines.append("Nightly weight updates:")

    if weight_updates:
        arrow_map = {"up": "↑", "down": "↓", "flat": "→"}
        for agent, multiplier, direction in weight_updates[:6]:  # Cap at 6 agents
            arrow = arrow_map.get(direction.lower(), "→")
            lines.append(f"• {agent}: {multiplier:.2f}x {arrow}")

    return "\n".join(lines)


# ============================================================================
# MULTI-MESSAGE COMPOSERS — Return list[str] for burst sends
# ============================================================================

def burst_signal_with_market(
    direction: str,
    target: float | None = None,
    confidence: str | None = None,
    reasons: list[str] | None = None,
    price: float | None = None,
    price_change_pct: float | None = None,
    price_range: tuple[float, float] | None = None,
    volume_context: str | None = None,
    timestamp_et: str | None = None,
) -> list[str]:
    """Compose a two-message burst: SIGNAL + MARKET context.

    Sends signals and market context as separate messages for better Telegram rhythm.

    Returns:
        [signal_message, market_message]
    """
    signal_msg = format_signal_burst(direction, target, confidence, reasons, timestamp_et)
    market_msg = format_market_burst(price, price_change_pct, price_range, volume_context, timestamp_et)
    return [signal_msg, market_msg]
