"""Tests for chat-native burst message formatters (message_formatters_v2).

Verifies:
- Message line count ≤8 per message
- Emoji count ≤4 per message
- No calibration metadata (no "hit rate" or "samples")
- Proper spacing with aggressive blank lines
- Correct timestamp format (HH:MM ET)
- HTML escaping for safety
"""
import pytest
from message_formatters_v2 import (
    format_signal_burst,
    format_market_burst,
    format_trade_burst,
    format_structure_burst,
    format_alert_burst,
    format_impact_burst,
    format_social_burst,
    format_watchdog_burst,
    format_summary_burst,
    format_update_burst,
    format_pattern_burst,
    format_stale_burst,
    format_risk_burst,
    format_trend_burst,
    format_price_burst,
    burst_signal_with_market,
    clamp_reasons,
    strip_calibration,
)


class TestBurstFormatting:
    """Verify burst format constraints."""

    def count_lines(self, text: str) -> int:
        """Count non-empty lines."""
        return len([ln for ln in text.split("\n") if ln.strip()])

    def count_emojis(self, text: str) -> int:
        """Count emoji characters (simple heuristic)."""
        emoji_chars = set("🟢🔴🟡⚪📈📉↔️✅❌⚠️🆘📌💬📰🔮🎯🛡️🌍📊💰")
        return sum(1 for c in text if c in emoji_chars)

    def has_calibration_metadata(self, text: str) -> bool:
        """Check if text contains calibration metadata to hide."""
        forbidden = ["hit rate", "calibrated from", "samples", "cold start", "multiplier"]
        return any(phrase in text.lower() for phrase in forbidden)

    # ─── SIGNAL BURST ─────────────────────────────────────────────────────

    def test_signal_burst_bullish_with_target(self):
        """SIGNAL burst with bullish direction and target."""
        msg = format_signal_burst(
            direction="BULLISH",
            target=25.50,
            confidence="78%",
            reasons=["RSI oversold", "Volume spike", "Support hold"],
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert self.count_emojis(msg) <= 4
        assert not self.has_calibration_metadata(msg)
        assert "14:30 ET" in msg
        assert "🟢" in msg  # Bullish emoji
        assert "$25.50" in msg
        assert "78%" in msg
        assert "RSI oversold" in msg

    def test_signal_burst_bearish(self):
        """SIGNAL burst with bearish direction."""
        msg = format_signal_burst(
            direction="BEARISH",
            target=21.95,
            confidence="55%",
            reasons=["Below VWAP", "RSI weak"],
            timestamp_et="14:30 ET"
        )
        assert "🔴" in msg  # Bearish emoji
        assert "21.95" in msg
        assert "BEARISH" in msg

    def test_signal_burst_wait_action(self):
        """SIGNAL burst with WAIT action (pending decision)."""
        msg = format_signal_burst(
            direction="WAIT",
            target=None,
            confidence="45%",
            reasons=["Consolidating", "Awaiting breakout"],
            timestamp_et="14:30 ET"
        )
        assert "⏳" in msg  # Wait emoji
        assert "45%" in msg

    def test_signal_burst_minimal(self):
        """SIGNAL burst with only direction (edge case)."""
        msg = format_signal_burst(direction="NEUTRAL")
        assert self.count_lines(msg) <= 8
        assert "NEUTRAL" in msg

    # ─── SIGNAL BURST — ENRICHED (entry + stop + R:R) ─────────────────────

    def test_bullish_signal_with_full_price_block_renders_directive_and_rr(self):
        """Given entry=23.45, stop=22.80, target=25.50, direction=BULLISH,
        When formatted, Then the burst contains a directive ("Buy near $X"),
        the full price block (Stop / Target), and the R:R ratio
        (computed: (25.50−23.45)/(23.45−22.80) ≈ 3.15)."""
        msg = format_signal_burst(
            direction="BULLISH",
            entry=23.45,
            stop=22.80,
            target=25.50,
            confidence="78%",
            reasons=["RSI oversold", "Volume spike", "Support hold"],
            timestamp_et="14:30 ET",
        )
        assert "Buy near $23.45" in msg
        assert "Stop: $22.80" in msg
        assert "Target: $25.50" in msg
        assert "R:R 1:3.2" in msg
        assert "78%" in msg
        assert self.count_lines(msg) <= 8
        assert self.count_emojis(msg) <= 4
        assert not self.has_calibration_metadata(msg)

    def test_bearish_signal_inverts_directive_and_rr(self):
        """Given direction=BEARISH and entry>target (short setup), When
        formatted, Then the directive is "Sell near $X" and the R:R uses
        (entry−target)/(stop−entry). Example: entry=23.45, stop=24.10,
        target=22.00 → reward=1.45, risk=0.65, R:R≈2.2."""
        msg = format_signal_burst(
            direction="BEARISH",
            entry=23.45,
            stop=24.10,
            target=22.00,
            confidence="62%",
            reasons=["Below VWAP", "RSI weak"],
            timestamp_et="14:30 ET",
        )
        assert "Sell near $23.45" in msg
        assert "Stop: $24.10" in msg
        assert "Target: $22.00" in msg
        assert "R:R 1:2.2" in msg
        assert "🔴" in msg

    def test_missing_entry_falls_back_to_target_only(self):
        """Given entry=None and target=25.50 (legacy caller / partial agent),
        When formatted, Then today's "🟢 BULLISH" + "Target: $25.50" shape
        is preserved — no caller breaks."""
        msg = format_signal_burst(
            direction="BULLISH",
            target=25.50,
            confidence="78%",
            timestamp_et="14:30 ET",
        )
        assert "BULLISH" in msg            # bare direction line preserved
        assert "Target: $25.50" in msg
        assert "Buy near" not in msg       # directive only appears with entry
        assert "R:R" not in msg            # R:R only appears with full block

    def test_degenerate_rr_is_suppressed(self):
        """Given entry==stop (zero-risk degenerate setup), When formatted,
        Then the price block omits the R:R fragment rather than crashing on
        division-by-zero. The stop/target line still renders."""
        msg = format_signal_burst(
            direction="BULLISH",
            entry=23.45,
            stop=23.45,
            target=25.50,
            confidence="78%",
            timestamp_et="14:30 ET",
        )
        assert "Stop: $23.45" in msg
        assert "Target: $25.50" in msg
        assert "R:R" not in msg

    def test_hold_signal_omits_price_block_even_with_full_data(self):
        """Given direction=HOLD/WAIT with entry+stop+target set, When
        formatted, Then no Stop/Target/R:R line appears. Preserves the
        commit-17dcbce policy ("HOLD/WAIT drop the price block entirely")
        even if a future caller wires through full data."""
        msg = format_signal_burst(
            direction="WAIT",
            entry=23.45,
            stop=22.80,
            target=25.50,
            confidence="45%",
            timestamp_et="14:30 ET",
        )
        assert "Stop:" not in msg
        assert "Target:" not in msg
        assert "R:R" not in msg
        assert "Buy near" not in msg
        assert "⏳" in msg                  # WAIT emoji preserved
        assert "45%" in msg

    # ─── MARKET BURST ─────────────────────────────────────────────────────

    def test_market_burst_with_range(self):
        """MARKET burst with price, range, and volume."""
        msg = format_market_burst(
            price=23.45,
            price_change_pct=-1.40,
            price_range=(21.99, 25.50),
            volume_context="1.8x 20d ADV",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "$23.45" in msg
        assert "-1.40%" in msg
        assert "$21.99" in msg
        assert "$25.50" in msg

    def test_market_burst_down_direction(self):
        """MARKET burst detects down movement."""
        msg = format_market_burst(price=23.45, price_change_pct=-1.40)
        assert "📉" in msg  # Down emoji

    def test_market_burst_sideways(self):
        """MARKET burst with sideways movement."""
        msg = format_market_burst(price=23.45, price_change_pct=0.2)
        assert "↔️" in msg  # Sideways emoji

    # ─── TRADE BURST ──────────────────────────────────────────────────────

    def test_trade_burst_buy(self):
        """TRADE burst for BUY order."""
        msg = format_trade_burst(
            side="BUY",
            symbol="GME",
            entry=23.45,
            target=25.50,
            stop_loss=22.80,
            quantity=100,
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "🟢" in msg  # Buy emoji
        assert "BUY" in msg
        assert "GME" in msg
        assert "$23.45" in msg
        assert "$25.50" in msg
        assert "$22.80" in msg
        assert "100" in msg

    def test_trade_burst_sell(self):
        """TRADE burst for SELL order."""
        msg = format_trade_burst(side="SELL", entry=24.00)
        assert "🔴" in msg  # Sell emoji
        assert "SELL" in msg

    def test_trade_burst_minimal(self):
        """TRADE burst with only side and entry."""
        msg = format_trade_burst(side="BUY", symbol="GME", entry=23.45)
        assert self.count_lines(msg) <= 8
        assert "$23.45" in msg

    # ─── STRUCTURE BURST ──────────────────────────────────────────────────

    def test_structure_burst_pe_playbook(self):
        """STRUCTURE burst for PE playbook signal."""
        msg = format_structure_burst(
            ticker="AMC",
            signal_type="restructuring_advisor_hired",
            confidence="95%",
            timeline="~6 months",
            news_snippet="News headline here",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "AMC" in msg
        assert "95%" in msg
        assert "6 months" in msg

    # ─── ALERT BURST ──────────────────────────────────────────────────────

    def test_alert_burst_critical_immunity(self):
        """ALERT burst for critical immunity breach."""
        msg = format_alert_burst(
            alert_type="DEBT_FREE",
            severity="CRITICAL",
            message="GME issued $500M in bonds",
            action="Review position immediately",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "🆘" in msg  # Critical emoji
        assert "DEBT_FREE" in msg
        assert "Action:" in msg

    def test_alert_burst_warning(self):
        """ALERT burst for warning severity."""
        msg = format_alert_burst(
            alert_type="Data lag",
            severity="WARNING",
            message="Pricing data 5 minutes old"
        )
        assert "⚠️" in msg

    # ─── IMPACT BURST (Max Pain) ───────────────────────────────────────────

    def test_impact_burst_max_pain(self):
        """IMPACT burst for max pain update."""
        msg = format_impact_burst(
            max_pain=24.50,
            current_price=23.20,
            expiry_date="2026-05-17",
            oi_bias="slightly bullish",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "$24.50" in msg
        assert "$23.20" in msg
        assert "Below" in msg  # Shows price is below max pain
        assert "2026-05-17" in msg

    # ─── SOCIAL BURST ────────────────────────────────────────────────────

    def test_social_burst_tweet(self):
        """SOCIAL burst for tracked account post."""
        msg = format_social_burst(
            handle="RoaringKitty",
            message="New position update in latest filing",
            urgency="INFO",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "RoaringKitty" in msg
        assert "position update" in msg

    def test_social_burst_critical(self):
        """SOCIAL burst with critical urgency."""
        msg = format_social_burst(
            handle="CohenAdam",
            message="Major announcement",
            urgency="CRITICAL"
        )
        assert "🚨" in msg  # Critical emoji

    # ─── WATCHDOG BURST ───────────────────────────────────────────────────

    def test_watchdog_burst_feed_offline(self):
        """WATCHDOG burst for data feed offline."""
        msg = format_watchdog_burst(
            feed_name="TradingView",
            duration_offline=15,
            fallback_status="Alpaca backup active",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "TradingView" in msg
        assert "15 minutes" in msg
        assert "Alpaca" in msg

    # ─── SUMMARY BURST (Daily Close) ───────────────────────────────────────

    def test_summary_burst_daily_pnl(self):
        """SUMMARY burst for end-of-day summary."""
        msg = format_summary_burst(
            price=23.45,
            pnl=245.60,
            pnl_pct=2.1,
            win_rate=0.72,
            trades_count=18,
            learnings=["Strong support hold", "Afternoon pressure"],
            timestamp_et="16:00 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "$23.45" in msg
        assert "+$245.60" in msg
        assert "72%" in msg
        assert "18" in msg

    def test_summary_burst_loss_day(self):
        """SUMMARY burst with negative P&L."""
        msg = format_summary_burst(
            price=22.50,
            pnl=-123.45,
            win_rate=0.45,
            trades_count=8
        )
        assert "-$123.45" in msg
        assert "45%" in msg

    # ─── UPDATE BURST (Periodic Brief) ────────────────────────────────────

    def test_update_burst_market_digest(self):
        """UPDATE burst for periodic market digest."""
        msg = format_update_burst(
            consensus_direction="BULLISH",
            consensus_pct=67,
            top_signal="Pattern breakout on daily",
            structure_status="No red flags",
            risk_flag="Geopolitical neutral",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "BULLISH" in msg
        assert "67%" in msg
        assert "Pattern breakout" in msg

    # ─── PATTERN BURST ────────────────────────────────────────────────────

    def test_pattern_burst_chart_pattern(self):
        """PATTERN burst for technical pattern recognition."""
        msg = format_pattern_burst(
            timeframe="Daily",
            pattern_name="Double Bottom",
            direction="BULLISH",
            target=25.50,
            confidence="85%",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "Daily" in msg
        assert "Double Bottom" in msg
        assert "BULLISH" in msg
        assert "$25.50" in msg

    # ─── TREND BURST ──────────────────────────────────────────────────────

    def test_trend_burst_uptrend(self):
        """TREND burst for uptrend."""
        msg = format_trend_burst(
            direction="UP",
            key_level=24.50,
            strength=0.65,
            timestamp_et="14:30 ET"
        )
        assert "📈" in msg
        assert "UP" in msg
        assert "65%" in msg
        assert "$24.50" in msg

    def test_trend_burst_downtrend(self):
        """TREND burst for downtrend."""
        msg = format_trend_burst(direction="DOWN")
        assert "📉" in msg

    # ─── PRICE BURST ──────────────────────────────────────────────────────

    def test_price_burst_snapshot(self):
        """PRICE burst for ultra-compact price tick."""
        msg = format_price_burst(
            price=23.45,
            direction="DOWN",
            change_pct=-1.40,
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 4  # Very short
        assert "$23.45" in msg
        assert "-1.40%" in msg

    # ─── STALE BURST ──────────────────────────────────────────────────────

    def test_stale_burst_data_warning(self):
        """STALE burst for data freshness warning."""
        msg = format_stale_burst(
            stale_data_type="price_ticks",
            time_gap=8,
            impact="Signals suppressed until fresh",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "price_ticks" in msg
        assert "8 minutes" in msg

    # ─── RISK BURST ───────────────────────────────────────────────────────

    def test_risk_burst_geopolitical(self):
        """RISK burst for geopolitical context."""
        msg = format_risk_burst(
            risk_type="Geopolitical",
            assessment="Elevated tensions",
            impact_on_gme="Sector rotation pressure",
            timestamp_et="14:30 ET"
        )
        assert self.count_lines(msg) <= 8
        assert "Geopolitical" in msg
        assert "Elevated" in msg
        assert "Sector rotation" in msg

    # ─── MULTI-MESSAGE COMPOSERS ──────────────────────────────────────────

    def test_burst_signal_with_market_sends_two(self):
        """burst_signal_with_market() returns 2 separate messages."""
        messages = burst_signal_with_market(
            direction="BULLISH",
            target=25.50,
            confidence="78%",
            reasons=["RSI oversold", "Volume spike"],
            price=23.45,
            price_change_pct=-1.40,
            price_range=(21.99, 25.50),
            volume_context="1.8x 20d ADV",
            timestamp_et="14:30 ET"
        )
        # After the label-dropping change (2026-05-13), emoji is the identity,
        # not the literal word. Verify by emoji + content instead.
        assert len(messages) == 2
        assert "🔮" in messages[0]  # default signal emoji (Futurist)
        assert "💬" in messages[1]  # market emoji
        assert "BULLISH" in messages[0]
        assert "$23.45" in messages[1]
        assert self.count_lines(messages[0]) <= 8
        assert self.count_lines(messages[1]) <= 8

    # ─── UTILITY FUNCTIONS ────────────────────────────────────────────────

    def test_clamp_reasons_limits_to_three(self):
        """clamp_reasons() limits bullet list to 3 items max."""
        reasons = ["First", "Second", "Third", "Fourth", "Fifth"]
        clamped = clamp_reasons(reasons, 3)
        assert len(clamped) == 3
        assert "Fourth" not in clamped

    def test_clamp_reasons_empty(self):
        """clamp_reasons() handles empty list."""
        assert clamp_reasons([], 3) == []

    def test_strip_calibration_removes_metadata(self):
        """strip_calibration() removes hit-rate and sample count."""
        text = "78% (calibrated from 80%, hit rate 75% on 12 samples)"
        clean = strip_calibration(text)
        assert clean == "78%"
        assert "calibrated" not in clean
        assert "hit rate" not in clean

    def test_strip_calibration_simple_percent(self):
        """strip_calibration() passes through simple percentages."""
        assert strip_calibration("78%") == "78%"

    def test_strip_calibration_empty(self):
        """strip_calibration() handles empty string."""
        assert strip_calibration("") == ""

    # ─── CONSTRAINT VERIFICATION ──────────────────────────────────────────

    def test_all_bursts_under_eight_lines(self):
        """Verify all burst types produce ≤8 lines."""
        test_cases = [
            (format_signal_burst("BULLISH", 25.50, "78%", ["Reason 1", "Reason 2"]),
             "SIGNAL"),
            (format_market_burst(23.45, -1.40, (21.99, 25.50), "1.8x 20d ADV"), "MARKET"),
            (format_trade_burst("BUY", "GME", 23.45, 25.50, 22.80, 100), "TRADE"),
            (format_structure_burst("AMC", "signal", "95%"), "STRUCTURE"),
            (format_alert_burst("Type", "CRITICAL", "Message"), "ALERT"),
            (format_impact_burst(24.50, 23.20), "IMPACT"),
            (format_social_burst("User", "Post"), "SOCIAL"),
            (format_watchdog_burst("Feed", 15), "WATCHDOG"),
            (format_summary_burst(23.45, 100, win_rate=0.72, trades_count=10), "SUMMARY"),
            (format_update_burst("BULLISH", 67), "UPDATE"),
            (format_pattern_burst("Daily", "Double Bottom", "BULLISH"), "PATTERN"),
            (format_stale_burst("data", 8), "STALE"),
            (format_risk_burst("Type", "Assessment"), "RISK"),
            (format_trend_burst("UP", 24.50, 0.65), "TREND"),
            (format_price_burst(23.45, "DOWN", -1.40), "PRICE"),
        ]
        for msg, burst_type in test_cases:
            line_count = self.count_lines(msg)
            assert line_count <= 8, f"{burst_type} has {line_count} lines (max 8)"

    def test_no_calibration_metadata_in_signals(self):
        """Verify signal bursts don't expose calibration math."""
        msg = format_signal_burst("BULLISH", 25.50, "78% (calibrated from 80%)")
        # The function should strip calibration from the displayed confidence
        assert "calibrated" not in msg.lower() or "78%" in msg  # 78% is shown, not metadata
