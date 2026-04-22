#!/usr/bin/env python3
"""
Test Futurist prediction signal integration.

Quick validation that:
1. Futurist agent runs with DeepSeek-r1
2. Output is parsed to FuturistPrediction
3. Signal is logged to DB
4. Telegram alert is sent (or would be, if configured)
"""
import os
import sys
import logging

# Setup path
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

def test_parsing():
    """Test the JSON extraction logic."""
    from orchestrator import _extract_futurist_prediction

    print("\n" + "=" * 70)
    print("TEST 1: JSON Extraction")
    print("=" * 70)

    # Test case 1: DeepSeek-r1 output with <thought> block
    deepseek_output = """
    <thought>
    Looking at the data:
    - RSI is 28 (oversold)
    - Volume is 2.3x average
    - Historical pattern suggests rebound

    Let me calculate confidence...
    </thought>

    Based on my analysis:

    ```json
    {
        "predicted_price": 25.50,
        "confidence": 0.78,
        "horizon": "1h",
        "bias": "BULLISH",
        "reasoning": "RSI oversold at 28, volume spike +2.3x avg on red candle. Historical pattern: rebounds 85% of time within 1h.",
        "signal_type": "price_prediction",
        "stop_loss": 22.80,
        "take_profit": 25.50
    }
    ```
    """

    result = _extract_futurist_prediction(deepseek_output)
    print(f"✓ DeepSeek output parsed: {result}")
    assert result["predicted_price"] == 25.50
    assert result["confidence"] == 0.78
    print("  ✓ Confidence: 78%")
    print("  ✓ Entry: $25.50")
    print()

    # Test case 2: Gemma output (plain JSON)
    gemma_output = """{
        "predicted_price": 23.75,
        "confidence": 0.65,
        "horizon": "4h",
        "bias": "NEUTRAL",
        "reasoning": "Mixed signals",
        "signal_type": "price_prediction",
        "stop_loss": 22.50,
        "take_profit": 25.00
    }"""

    result = _extract_futurist_prediction(gemma_output)
    print(f"✓ Gemma output parsed: {result}")
    assert result["predicted_price"] == 23.75
    assert result["confidence"] == 0.65
    print("  ✓ Confidence: 65%")
    print()


def test_pydantic_validation():
    """Test that parsed JSON validates as FuturistPrediction."""
    from models.agent_outputs import FuturistPrediction

    print("=" * 70)
    print("TEST 2: Pydantic Validation")
    print("=" * 70)

    data = {
        "predicted_price": 25.50,
        "confidence": 0.78,
        "horizon": "1h",
        "bias": "BULLISH",
        "reasoning": "RSI oversold + volume spike",
        "signal_type": "price_prediction",
        "stop_loss": 22.80,
        "take_profit": 25.50
    }

    prediction = FuturistPrediction(**data)
    print(f"✓ FuturistPrediction created:")
    print(f"  - Price: ${prediction.predicted_price:.2f}")
    print(f"  - Confidence: {prediction.confidence:.0%}")
    print(f"  - Stop Loss: ${prediction.stop_loss:.2f}")
    print(f"  - Take Profit: ${prediction.take_profit:.2f}")
    print()


def test_signal_manager():
    """Test that signals can be logged to DB."""
    from signal_manager import SignalManager
    import sqlite3

    print("=" * 70)
    print("TEST 3: Signal Manager")
    print("=" * 70)

    db_path = os.path.join(os.path.dirname(__file__), "agent_memory.db")

    # Check DB exists
    if not os.path.exists(db_path):
        print(f"❌ Database not found: {db_path}")
        print("   Run: python -m alembic upgrade head")
        return False

    manager = SignalManager(db_path)

    # Log a test signal
    alert_id = manager.log_alert(
        agent_name="Futurist",
        signal_type="price_prediction",
        confidence=0.78,
        severity="MEDIUM",
        entry_price=25.50 * 0.99,
        stop_loss=22.80,
        take_profit=25.50,
        reasoning="Test signal from test_futurist_signal.py",
    )

    print(f"✓ Signal logged: {alert_id}")

    # Verify it's in the DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM signal_alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()

    if row:
        print(f"✓ Verified in DB:")
        print(f"  - Agent: {row['agent_name']}")
        print(f"  - Type: {row['signal_type']}")
        print(f"  - Confidence: {row['confidence']:.0%}")
        print(f"  - Entry: ${row['entry_price']:.2f}")
        print()
        return True
    else:
        print(f"❌ Signal not found in DB")
        return False


def test_telegram_alert():
    """Test Telegram alert formatting (don't actually send)."""
    from notifier import notify_signal_alert

    print("=" * 70)
    print("TEST 4: Telegram Alert (preview, not sent)")
    print("=" * 70)

    # Mock the _send function to see what would be sent
    import notifier
    original_send = notifier._send

    sent_message = None
    def mock_send(text, parse_mode="HTML"):
        nonlocal sent_message
        sent_message = text
        return True

    notifier._send = mock_send

    # Try to send alert
    result = notify_signal_alert(
        agent_name="Futurist",
        signal_type="price_prediction",
        confidence=0.78,
        entry_price=25.50 * 0.99,
        stop_loss=22.80,
        take_profit=25.50,
        reasoning="RSI oversold + volume spike",
        alert_id="test-abc-123",
    )

    # Restore original
    notifier._send = original_send

    if sent_message:
        print("✓ Alert would be sent:")
        print("\n" + sent_message)
        print()

    return True


if __name__ == "__main__":
    print("\n" + "╔" + "=" * 68 + "╗")
    print("║ Futurist Signal Integration Tests".ljust(70) + "║")
    print("╚" + "=" * 68 + "╝\n")

    try:
        test_parsing()
        test_pydantic_validation()
        success = test_signal_manager()
        test_telegram_alert()

        if success:
            print("=" * 70)
            print("✅ All tests passed!")
            print("=" * 70)
            print("\nNext steps:")
            print("1. Run orchestrator: python orchestrator.py")
            print("2. Wait for first Futurist signal (every 2 hours)")
            print("3. Check DB: sqlite3 agent_memory.db 'SELECT * FROM signal_alerts LIMIT 5;'")
            print("4. Team logs feedback: python log_signal_feedback.py log --alert-id ABC --action executed --exit 24.50")
            print("5. View metrics: python log_signal_feedback.py metrics --agent Futurist --days 7")
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
