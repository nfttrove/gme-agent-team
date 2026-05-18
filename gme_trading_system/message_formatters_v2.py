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


def format_header(emoji: str, message_type: str | None = None, timestamp_et: str | None = None) -> str:
    """Format message header with emoji and timestamp.

    The emoji IS the agent identity — message_type labels (SIGNAL, MARKET, etc.)
    are dropped from the header to keep the feed clean. Kept as an optional
    parameter for API stability with existing tests; ignored at runtime.

    Args:
        emoji: Single emoji character (e.g., '🔮')
        message_type: Legacy/unused; preserved for back-compat
        timestamp_et: Optional timestamp in format "HH:MM ET"

    Returns:
        Formatted header string (e.g., "🔮 14:30 ET")
    """
    _ = message_type  # intentionally unused — emoji is the identity
    if timestamp_et:
        return f"{emoji} {timestamp_et}"
    return emoji


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
    emoji: str = "🔮",
    *,
    entry: float | None = None,
    stop: float | None = None,
) -> str:
    """Format a signal recommendation burst.

    Returns ~5–8 line message with directional read (Bullish/Bearish near $X),
    full price block (stop · target · R:R), confidence, and up to 3 bullet reasons.

    When `entry` is present and direction is BULLISH/BEARISH, the line is
    rewritten as a directional read ("🟢 Bullish near $23.45") and a compact
    price block is emitted on the next line. When `entry` or `stop` is missing,
    falls back to the bare "🟢 BULLISH" + "Target: $X" rendering for
    backward compatibility with legacy callers.

    HOLD/WAIT/NEUTRAL never get a price block — commit 17dcbce policy:
    the reader is not placing an order on a HOLD/WAIT so prices are noise.

    Args:
        emoji: Agent emoji to use as identity. Default 🔮 (Futurist) since
               structured signal alerts are almost always Futurist predictions.
               Callers can override for other agents.

    Example (BUY/SELL with full block):
        🔮 14:30 ET

        🟢 Bullish near $23.45
        Stop: $22.80 · Target: $25.50 · R:R 1:3.2
        Confidence: 78%

        • RSI oversold
        • Volume spike
        • Support hold
    """
    lines = [format_header(emoji, timestamp_et=timestamp_et), ""]

    dir_upper = direction.upper()
    dir_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪", "HOLD": "🟡", "WAIT": "⏳"}.get(
        dir_upper, "⚪"
    )

    # Directional signal with full price block: directive headline + compact
    # stop/target/R:R line. Triggers only when ALL three prices are present
    # and the call is BUY/SELL — preserves the HOLD/WAIT "no prices" policy.
    is_directional = dir_upper in ("BULLISH", "BEARISH")
    has_full_block = is_directional and entry is not None and stop is not None and target is not None

    if has_full_block:
        verb = "Bullish near" if dir_upper == "BULLISH" else "Bearish near"
        lines.append(f"{dir_emoji} {verb} ${entry:.2f}")

        price_parts = [f"Stop: ${stop:.2f}", f"Target: ${target:.2f}"]
        # Risk:Reward = reward / risk. For BULLISH: (target-entry)/(entry-stop).
        # For BEARISH: (entry-target)/(stop-entry). abs() handles both.
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk > 0:
            rr = reward / risk
            # Suppress degenerate ratios — they signal a bad setup, not info.
            if 0.1 <= rr <= 99:
                price_parts.append(f"R:R (risk:reward) 1:{rr:.1f}")
        lines.append(" · ".join(price_parts))
    else:
        # Fallback: bare direction line + optional Target. Legacy callers
        # (Pattern, anything without full price data) land here. HOLD/WAIT/
        # NEUTRAL also land here and intentionally skip the price block.
        lines.append(f"{dir_emoji} {direction}")
        if target and is_directional:
            lines.append(f"Target: ${target:.2f}")

    # Confidence
    if confidence:
        clean_conf = strip_calibration(confidence)
        lines.append(f"Confidence: {clean_conf}")

    # IF/THEN frame (only on full directional block — needs entry + stop).
    # "Waiting for" tells the reader the trigger; "Invalidated if" tells
    # them what would kill the setup. Mirrors the daily strategy brief
    # structure so the reader can act on the same scaffolding everywhere.
    if has_full_block:
        lines.append("")
        lines.append(f"⏳ WAITING FOR: Price to test ${entry:.2f}")
        lines.append(f"⚠️ INVALIDATED IF: Move past ${stop:.2f}")

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

    lines.append(f"Max Pain (strike where most options expire worthless): ${max_pain:.2f}")

    diff = current_price - max_pain
    # Direction emoji leads so the reader sees above/below max-pain before
    # the dollar figure. 🔻 = below (calls heavy / bearish for pin),
    # 🔺 = above (puts heavy / bullish for pin).
    diff_emoji = "🔻" if diff < 0 else "🔺"
    diff_word = "below" if diff < 0 else "above"
    lines.append(f"{diff_emoji} Current: ${current_price:.2f} ({diff_word} by ${abs(diff):.2f})")

    if oi_bias:
        lines.append(f"OI (open interest) Bias: {oi_bias}")

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

    Example (GME, DV score with bullets):
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

    # Bullets win over news snippet (preferred for GME DV score)
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


def format_standup_brief(
    timestamp_et: str,
    spot_price: float | None,
    trusted: list,
    muted: list,
    last_24h_total: int,
    last_24h_wins: int,
    last_24h_avg_pnl_pct: float | None,
    status_diff: str,
) -> str:
    """Verdict-first standup: who to listen to / who's muted / 24h trades / diff.

    trusted and muted are lists of standup_brief.AgentVerdict (duck-typed
    here to avoid an import cycle).
    """
    header_bits = ["🤖 <b>STANDUP</b>"]
    if spot_price is not None:
        header_bits.append(f"GME ${spot_price:.2f}")
    # get_ny_time_short() already includes "ET" suffix
    header_bits.append(timestamp_et)
    lines = [" · ".join(header_bits), ""]

    def _form_chip(v) -> str:
        """Build the '· 5/8 63% 7d ↑' suffix when we have 7d data.
        Empty string when 7d sample is zero (silent — no fake data)."""
        n_7d = getattr(v, "sample_size_7d", 0) or 0
        if n_7d < 1:
            return ""
        hits_7d = getattr(v, "hits_correct_7d", 0) or 0
        rate_7d = getattr(v, "hit_rate_7d", None)
        if rate_7d is None:
            return ""
        # Direction arrow: ↑ improving by ≥5pp, ↓ worsening by ≥5pp, → flat.
        # 5pp threshold filters noise — anything tighter is statistically
        # indistinguishable from the 30d baseline at typical n.
        delta = rate_7d - (v.hit_rate or 0)
        arrow = "↑" if delta >= 0.05 else "↓" if delta <= -0.05 else "→"
        return f" · 7d {hits_7d}/{n_7d} {rate_7d * 100:.0f}% {arrow}"

    # LISTEN bucket
    if not trusted:
        lines.append("⚠️ <b>NO TRUSTED AGENTS</b> — lean on price + structure, not signals")
    else:
        only = " · only agent passing" if len(trusted) == 1 else ""
        for v in trusted:
            pct = f"{(v.hit_rate or 0) * 100:.0f}%"
            lines.append(
                f"✅ <b>LISTEN: {escape_html(v.agent_name)}</b>"
            )
            lines.append(
                f"   {v.hits_correct}/{v.sample_size} calls right ({pct}){_form_chip(v)}{only}"
            )
            if v.small_sample:
                lines.append("   ⚠️ small sample — could be a hot streak")
            only = ""  # only flag the first one

    # MUTED bucket — collapse SHADOW + SUPPRESS into one block
    if muted:
        lines.append("")
        lines.append("🔕 <b>MUTED</b> <i>(no Telegram alerts from these):</i>")
        # Compute name padding for alignment. Per-row 🔇 prefix so muted
        # state signals at a glance before the reader parses the metrics.
        max_name = max(len(v.agent_name) for v in muted)
        for v in muted:
            pct = f"{(v.hit_rate or 0) * 100:.0f}%"
            stats = f"{v.hits_correct}/{v.sample_size}".rjust(7)
            lines.append(
                f"   🔇 {escape_html(v.agent_name).ljust(max_name)}  {stats}  ({pct}){_form_chip(v)} — {v.reason}"
            )

    # 24h paper trade summary — collapsed
    lines.append("")
    if last_24h_total:
        avg_chip = f" · avg {last_24h_avg_pnl_pct:+.1f}%" if last_24h_avg_pnl_pct is not None else ""
        lines.append(
            f"Last 24h: <b>{last_24h_wins} of {last_24h_total}</b> hit profit{avg_chip}"
        )
    else:
        lines.append("Last 24h: no paper trades closed")

    # Status diff line
    lines.append("")
    lines.append(f"📊 <i>Status: {escape_html(status_diff)}</i>")

    return "\n".join(lines)


def format_week_ahead(snapshot, timestamp_et: str | None = None) -> str:
    """Sunday-evening week-ahead preview — calendar + the week's anchors.

    snapshot is a week_ahead.WeekAheadSnapshot; kept duck-typed so this file
    doesn't import week_ahead and create a cycle.
    """
    today = snapshot.today
    friday = snapshot.next_friday
    lines = [
        f"🔭 <b>GME · Week ahead</b>",
        f"<i>{today.strftime('%a %d %b %Y')}</i>",
        "",
    ]

    lines.append(f"<b>Friday expiry:</b> {friday.strftime('%a %d %b')}")
    if snapshot.last_max_pain and snapshot.last_spot_at_snapshot:
        bias = f" · {escape_html(snapshot.last_oi_bias)} bias" if snapshot.last_oi_bias else ""
        lines.append(
            f"   Last snapshot ({escape_html(snapshot.last_snapshot_expiration or '')}): "
            f"max pain (strike where most options expire worthless) ${snapshot.last_max_pain:.2f} vs spot (last price) ${snapshot.last_spot_at_snapshot:.2f}{bias}"
        )
    lines.append("   <i>Fresh brief Monday 13:30 BST.</i>")
    lines.append("")

    lines.append("📅 <b>Calendar</b>")
    lines.append("   • Mon 13:30 BST — options brief")
    lines.append(f"   • Fri close — {snapshot.trading_days_this_week} trading days this week")
    lines.append("")

    if snapshot.earnings_days_away is not None and snapshot.earnings_days_away <= EARNINGS_HORIZON_DAYS_DISPLAY:
        earnings_dt = snapshot.next_earnings_date or ""
        lines.append(
            f"💼 <b>Earnings:</b> {escape_html(earnings_dt[:10])} "
            f"({snapshot.earnings_days_away} days)"
        )
        lines.append("")

    lines.append("<i>Have a calm Sunday.</i>")
    return "\n".join(lines)


EARNINGS_HORIZON_DAYS_DISPLAY = 30  # only surface earnings if within this many days


_VOL_REGIME_EMOJI = {"elevated": "🌡", "subdued": "❄️", "in line": "📏"}


def format_options_brief(
    expiration: str,
    spot_price: float,
    candidates: list[dict],
    candidate_personas: list[tuple[str, str]],
    wow_diff: dict,
    gone: list[float],
    vol_predicted_pct: float | None,
    vol_long_term_pct: float | None,
    vol_regime: str,
    shares_takeaway: str,
    timestamp_et: str | None = None,
) -> str:
    """Compact Monday options brief — persona labels, WoW deltas, shares takeaway.

    candidates are the top-N call_contract_candidates payload rows. personas is
    parallel: same length, each (emoji, label) tuple from options_brief.persona_label.
    wow_diff is {strike: {is_new, oi_delta_pct, prev_oi}}; gone is strikes that
    fell off vs last week.
    """
    ts = timestamp_et or get_ny_time_short()
    vol_emoji = _VOL_REGIME_EMOJI.get(vol_regime, "📊")

    header_bits = [f"📊 <b>GME options</b> · exp {escape_html(expiration)}"]
    if vol_regime:
        header_bits.append(f"{vol_emoji} Vol {escape_html(vol_regime)}")
    header_bits.append(f"{ts} ET")
    lines = [" · ".join(header_bits), ""]

    if vol_predicted_pct is not None:
        vol_line = f"Spot (last price) <b>${spot_price:.2f}</b> · expecting ~{vol_predicted_pct:.2f}%/day"
        if vol_long_term_pct:
            vol_line += f" (90d {vol_long_term_pct:.2f}%)"
        lines.append(vol_line)
    else:
        lines.append(f"Spot (last price) <b>${spot_price:.2f}</b>")
    lines.append("")

    for c, (emoji, label) in zip(candidates, candidate_personas):
        strike = float(c["strike"])
        ask = float(c["ask"])
        iv = float(c.get("iv", 0.0))
        oi = int(c.get("open_interest") or 0)
        per_contract = ask * 100

        diff_chip = ""
        info = wow_diff.get(round(strike, 2)) if wow_diff else None
        if info:
            if info.get("is_new"):
                diff_chip = " · <b>NEW</b>"
            elif info.get("oi_delta_pct") is not None:
                delta = info["oi_delta_pct"]
                if abs(delta) >= 10:
                    arrow = "↑" if delta > 0 else "↓"
                    diff_chip = f" · OI (open interest) {arrow}{abs(delta):.0f}% WoW (week-over-week)"

        lines.append(f"{emoji} <b>${strike:.2f}</b> · <i>{label}</i>")
        lines.append(
            f"   ${per_contract:.0f}/contract · IV (implied vol) {iv:.0%} · OI (open interest) {oi}{diff_chip}"
        )

    if gone:
        lines.append("")
        gone_str = ", ".join(f"${s:.2f}" for s in gone[:3])
        lines.append(f"<i>Off the list this week:</i> {escape_html(gone_str)}")

    if shares_takeaway:
        lines.append("")
        lines.append(f"📈 <b>For shares:</b> {escape_html(shares_takeaway)}")

    lines.append("")
    lines.append("<i>Not an execution rec.</i>")
    return "\n".join(lines)
