#!/usr/bin/env python3
"""Test script for Telegram burst messages.

Run this to send example burst-format messages to your Telegram chat
and verify the 1-second comprehension test on mobile.

Usage:
    python3 test_telegram_bursts.py

The script will send:
1. A SIGNAL burst (with market context)
2. A TRADE burst
3. A STRUCTURE burst
4. An IMPACT burst
5. A SUMMARY burst
6. An UPDATE burst

After sending, check your Telegram for:
- Message readability on mobile (line count ≤8)
- 1-second comprehension (can you understand each message in 1 sec?)
- Minimal emoji (3-4 per message)
- NYSE timestamp visible (HH:MM ET)
- No calibration metadata
"""
import os
import sys

# Add gme_trading_system to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'gme_trading_system'))

from notifier import (
    notify_signal_alert,
    notify_trade,
    notify_cto_alert,
    notify_max_pain,
    notify_daily_summary,
    notify_periodic_brief,
)


def test_signal_alert():
    """Test signal alert burst."""
    print("📍 Sending SIGNAL burst...")
    notify_signal_alert(
        agent_name="Futurist",
        signal_type="price_prediction",
        confidence=0.78,
        entry_price=23.45,
        stop_loss=22.80,
        take_profit=25.50,
        reasoning="RSI oversold + volume spike on dip + support hold",
        alert_id="test123"
    )
    print("   ✅ Sent\n")


def test_trade():
    """Test trade approval burst."""
    print("📍 Sending TRADE burst...")
    notify_trade(
        action="BUY",
        price=23.45,
        confidence=0.73,
        stop_loss=22.80,
        take_profit=25.50,
        quantity=100,
        status="APPROVED"
    )
    print("   ✅ Sent\n")


def test_structure():
    """Test PE playbook structure burst."""
    print("📍 Sending STRUCTURE burst...")
    notify_cto_alert(
        ticker="AMC",
        signal_name="restructuring_advisor_hired",
        confidence=0.95,
        action="SHORT",
        timeline_months=6,
        headline="Breaking: AMC hires restructuring advisor"
    )
    print("   ✅ Sent\n")


def test_impact():
    """Test max pain impact burst."""
    print("📍 Sending IMPACT burst...")
    notify_max_pain(
        strike=24.50,
        current_price=23.20,
        friday_date="2026-05-17",
        net_oi_direction="slightly bullish"
    )
    print("   ✅ Sent\n")


def test_summary():
    """Test daily summary burst."""
    print("📍 Sending CLOSE burst...")
    notify_daily_summary(
        pnl=245.60,
        win_rate=0.72,
        trades=18,
        pred_error_pct=2.34,
        gme_close=23.45
    )
    print("   ✅ Sent\n")


def test_update():
    """Test periodic brief burst."""
    print("📍 Sending UPDATE burst...")
    notify_periodic_brief(
        price=23.45,
        pct_change=-1.2,
        consensus="BULLISH 67%",
        top_signal="Pattern breakout on daily",
        geo_risk="Geopolitical neutral",
        prediction="No structural red flags",
        options="Max pain $24.50 (expiry 2026-05-17)"
    )
    print("   ✅ Sent\n")


def main():
    """Send all test bursts to Telegram."""
    print("\n" + "="*70)
    print("TELEGRAM BURST FORMAT TEST")
    print("="*70 + "\n")
    print("Sending example burst messages...\n")

    try:
        test_signal_alert()
        test_trade()
        test_structure()
        test_impact()
        test_summary()
        test_update()

        print("="*70)
        print("✅ All test messages sent!")
        print("="*70)
        print("\nVerification checklist on Telegram:")
        print("  ☐ Message 1 (SIGNAL): Fits on mobile, 1-sec readable")
        print("  ☐ Message 2 (TRADE): Shows entry/target/stop clearly")
        print("  ☐ Message 3 (STRUCTURE): PE playbook signal readable")
        print("  ☐ Message 4 (IMPACT): Max pain vs current visible")
        print("  ☐ Message 5 (CLOSE): P&L and win rate clear")
        print("  ☐ Message 6 (UPDATE): Consensus and top signal visible")
        print("  ☐ All messages: NYSE time visible (HH:MM ET)")
        print("  ☐ All messages: Minimal emoji (≤4 per message)")
        print("  ☐ All messages: No calibration metadata shown\n")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nMake sure your .env has:")
        print("  TELEGRAM_BOT_TOKEN=your-token")
        print("  TELEGRAM_CHAT_ID=your-chat-id")
        sys.exit(1)


if __name__ == "__main__":
    main()
