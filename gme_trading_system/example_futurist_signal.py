#!/usr/bin/env python3
"""
Example: Wire Futurist agent to emit signals with confidence scores.

This shows the minimal changes needed to integrate the signal confidence + feedback loop.
"""
import os
import sys
import json
from datetime import datetime
from zoneinfo import ZoneInfo

# Add parent to path
sys.path.insert(0, os.path.dirname(__file__))

from signal_manager import SignalManager
from models.agent_outputs import FuturistPrediction
from notifier import notify_signal_alert
from orchestrator import DB_PATH, safe_kickoff, write_log
from crewai import Crew

ET = ZoneInfo("America/New_York")

# This is a mock agent + task setup for demonstration
# In production, you'd import from agents.py and tasks.py


def example_futurist_prediction() -> FuturistPrediction:
    """
    Simulated Futurist agent output.
    In production, this would be the result of:
        crew = Crew(agents=[futurist], tasks=[futurist_task])
        result = safe_kickoff(crew, timeout=300)
    """
    return FuturistPrediction(
        predicted_price=25.50,
        confidence=0.78,
        horizon="1h",
        bias="BULLISH",
        reasoning="RSI oversold at 28, volume spike +2.3x avg on red candle. Historical pattern: rebounds 85% of time within 1h. Price ~$0.70 resistance at $25.50.",
        signal_type="price_prediction",
        stop_loss=22.80,
        take_profit=25.50,
    )


def run_futurist_with_signals():
    """
    Updated Futurist cycle with signal confidence + feedback loop.

    Changes from old flow:
      OLD:   agent output → write to agent_logs → silence
      NEW:   agent output → parse to Pydantic → log signal → send alert → await feedback
    """
    print("[Futurist] Starting cycle...")

    # Step 1: Get agent output (simulated; production: crew.kickoff())
    try:
        # In production:
        # crew = Crew(agents=[futurist], tasks=[futurist_task])
        # result = safe_kickoff(crew, timeout=300)
        # prediction = parse_json_or_llm_extract(result)

        prediction = example_futurist_prediction()
        print(f"  ✓ Agent output parsed: {prediction.signal_type} @ {prediction.predicted_price:.2f}, confidence {prediction.confidence:.0%}")
    except Exception as e:
        print(f"  ✗ Failed to parse agent output: {e}")
        return

    # Step 2: Log to agent_logs (traditional logging)
    agent_output_text = f"Prediction: ${prediction.predicted_price:.2f} in {prediction.horizon}. Confidence: {prediction.confidence:.0%}. Reasoning: {prediction.reasoning}"
    write_log("Futurist", agent_output_text, "price_prediction", status="ok")

    # Step 3: Log signal alert (NEW)
    manager = SignalManager(DB_PATH)
    try:
        alert_id = manager.log_alert(
            agent_name="Futurist",
            signal_type=prediction.signal_type,
            confidence=prediction.confidence,
            severity="HIGH" if prediction.confidence >= 0.80 else ("MEDIUM" if prediction.confidence >= 0.65 else "LOW"),
            entry_price=prediction.predicted_price * 0.99,  # 1% slippage allowance
            stop_loss=prediction.stop_loss,
            take_profit=prediction.take_profit,
            reasoning=prediction.reasoning,
        )
        print(f"  ✓ Signal logged: {alert_id[:8]}")
    except Exception as e:
        print(f"  ✗ Failed to log signal: {e}")
        return

    # Step 4: Send Telegram alert (NEW)
    try:
        notify_signal_alert(
            agent_name="Futurist",
            signal_type=prediction.signal_type,
            confidence=prediction.confidence,
            entry_price=prediction.predicted_price * 0.99,
            stop_loss=prediction.stop_loss,
            take_profit=prediction.take_profit,
            reasoning=prediction.reasoning,
            alert_id=alert_id,
        )
        print(f"  ✓ Telegram alert sent")
    except Exception as e:
        print(f"  ⚠️  Telegram alert failed (non-critical): {e}")

    print(f"\n[Futurist] Cycle complete. Team will now decide: execute/ignore/miss.")
    print(f"[Futurist] To log feedback:")
    print(f"    python log_signal_feedback.py log --alert-id {alert_id[:8]} --action executed --entry 23.45 --exit 24.50 --member Alice")
    print()


def example_feedback_flow():
    """
    Simulated team feedback + learning loop.

    Shows how signal metrics improve as team logs decisions.
    """
    print("\n" + "=" * 70)
    print("FEEDBACK FLOW EXAMPLE")
    print("=" * 70 + "\n")

    manager = SignalManager(DB_PATH)

    # Scenario: Team executed the Futurist signal
    print("[Team] Received Futurist alert: BUY $23.45, Target $25.50")
    print("[Team] Decision: EXECUTE (price hit $24.50, target didn't reach)")
    print()

    # Simulate getting a recent alert
    recent_alerts = manager.get_recent_alerts(limit=1, agent_name="Futurist")
    if not recent_alerts:
        print("[Demo] No recent Futurist alerts in database. Skipping feedback simulation.\n")
        return

    alert = recent_alerts[0]
    alert_id = alert["id"]

    print(f"[Futurist] Alert ID: {alert_id[:8]}")
    print(f"[Futurist] Confidence: {alert['confidence']:.0%}, Entry: ${alert['entry_price']:.2f}, Target: ${alert['take_profit']:.2f}")
    print()

    # Team logs execution
    try:
        feedback_id = manager.log_feedback(
            alert_id=alert_id,
            action_taken="executed",
            entry_price=alert["entry_price"],
            exit_price=24.50,
            quantity=100,
            team_member="Alice",
            team_notes="Strong volume at support, exited at resistance",
        )
        print(f"[Feedback] Logged: {feedback_id[:8]}")
        profit_pct = ((24.50 - alert["entry_price"]) / alert["entry_price"]) * 100
        print(f"[Feedback] P&L: +{profit_pct:.2f}% ({profit_pct > 0 and 'WIN' or 'LOSS'})")
        print()
    except Exception as e:
        print(f"[Feedback] Error: {e}\n")
        return

    # System computes metrics
    print("[System] Computing signal metrics for Futurist...")
    metrics_result = manager.get_signal_metrics(agent_name="Futurist", days=7)

    if "error" not in metrics_result:
        for m in metrics_result["metrics"]:
            print(f"  Agent: {m['agent']}")
            print(f"  Signal Type: {m['signal_type']}")
            print(f"  Total Alerts: {m['total_alerts']}")
            print(f"  Execution Rate: {m['execution_rate']:.0%}")
            print(f"  Win Rate: {m['win_rate']:.0%}")
            print(f"  Avg P&L: {m['avg_pnl_pct']:+.2f}%")
        print()

    # Agents see metrics → adjust next cycle
    print("[Futurist] Saw metrics: 78% confidence signals have 71% win rate.")
    print("[Futurist] Insight: When I'm 75-80% sure, I'm right 2 of 3 times.")
    print("[Futurist] Action: Tighten reasoning filters, only fire when >75% confident.")
    print()


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("SIGNAL CONFIDENCE + FEEDBACK LOOP EXAMPLE")
    print("=" * 70 + "\n")

    # Step 1: Run agent with signal emission
    run_futurist_with_signals()

    # Step 2: Show feedback flow (if alerts exist)
    example_feedback_flow()

    # Step 3: Show CLI usage
    print("\n" + "=" * 70)
    print("NEXT: TEAM USES CLI TO LOG DECISIONS")
    print("=" * 70 + "\n")

    print("View recent alerts:")
    print("  $ python log_signal_feedback.py recent --limit 10\n")

    print("Log an execution:")
    print("  $ python log_signal_feedback.py log \\")
    print("      --alert-id ABC123 \\")
    print("      --action executed \\")
    print("      --entry 23.45 \\")
    print("      --exit 24.50 \\")
    print("      --member Alice\n")

    print("View signal metrics:")
    print("  $ python log_signal_feedback.py metrics --agent Futurist --days 7\n")

    print("=" * 70)
