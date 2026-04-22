# Signal Confidence & Feedback Loop — Implementation Guide

## Overview

This system adds **signal confidence scoring** and **team feedback tracking** to close the loop between agents and execution.

**The flow:**
1. Agent outputs a signal with `confidence` (0–1.0) + `signal_type`
2. Signal is logged to `signal_alerts` table with risk/reward params
3. Telegram alert is sent with clarity score + actionable entry/stop/target
4. Team executes (or ignores/misses) and logs feedback
5. System tracks win rate per signal type → agents improve

## Files Added/Modified

| File | Purpose |
|------|---------|
| `alembic/versions/2026_04_22_*.py` | Migration: `signal_alerts` + `signal_feedback` tables |
| `models/agent_outputs.py` | Updated Pydantic models: added `confidence`, `signal_type`, stop_loss/take_profit |
| `signal_manager.py` | Core: logs alerts, logs feedback, computes metrics |
| `log_signal_feedback.py` | CLI: team tool to log execution decisions |
| `notifier.py` | Added `notify_signal_alert()` with confidence + risk/reward display |

## Quick Start

### 1. Run the migration

```bash
cd gme_trading_system
python -m alembic upgrade head
```

Creates two new tables:
- `signal_alerts` — each alert sent with confidence, entry/stop/target
- `signal_feedback` — team response (executed/ignored/missed + P&L if filled)

### 2. Update an agent to output signals with confidence

Example: **Futurist agent** predicting a price move.

**Before:**
```python
def run_futurist_cycle():
    crew = Crew(agents=[futurist], tasks=[futurist_task])
    result = safe_kickoff(crew, timeout=300)
    write_log("Futurist", result, "price_prediction")
```

**After:**
```python
from signal_manager import SignalManager
from models.agent_outputs import FuturistPrediction
import json

def run_futurist_cycle():
    crew = Crew(agents=[futurist], tasks=[futurist_task])
    result = safe_kickoff(crew, timeout=300)
    write_log("Futurist", result, "price_prediction")

    # Parse agent output into structured signal
    try:
        # Agent output is free-form text; parse into FuturistPrediction
        prediction_dict = json.loads(result)  # OR use regex/LLM to extract
        prediction = FuturistPrediction(**prediction_dict)

        # Log signal with confidence
        manager = SignalManager(DB_PATH)
        alert_id = manager.log_alert(
            agent_name="Futurist",
            signal_type=prediction.signal_type,  # "price_prediction"
            confidence=prediction.confidence,     # 0.78
            severity="HIGH" if prediction.confidence >= 0.80 else "MEDIUM",
            entry_price=prediction.predicted_price * 0.99,  # 1% slippage
            stop_loss=prediction.stop_loss,
            take_profit=prediction.take_profit,
            reasoning=prediction.reasoning,
        )

        # Send Telegram alert with all details
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
    except Exception as e:
        log.warning(f"Failed to parse/signal Futurist output: {e}")
```

### 3. Team logs feedback via CLI

After executing (or ignoring) a signal:

```bash
# Log an execution
python log_signal_feedback.py log \
  --alert-id abc-123-def \
  --action executed \
  --entry 23.45 \
  --exit 24.50 \
  --member "Alice" \
  --notes "Strong volume support"

# Log an ignored signal
python log_signal_feedback.py log \
  --alert-id abc-123-def \
  --action ignored \
  --notes "Conflicting RSI divergence"

# Log a missed signal (was AFK)
python log_signal_feedback.py log \
  --alert-id abc-123-def \
  --action missed \
  --notes "Away from desk, fired at 10:30"
```

### 4. View signal metrics

```bash
# All signals (last 30 days)
python log_signal_feedback.py metrics

# By agent
python log_signal_feedback.py metrics --agent Futurist

# By signal type + days
python log_signal_feedback.py metrics --signal-type price_prediction --days 7

# Recent alerts
python log_signal_feedback.py recent --limit 20
python log_signal_feedback.py recent --agent Trendy
```

**Output example:**
```
📊 Signal Metrics (last 30 days)

Agent           Signal Type          Alerts  Exec % Win %   Avg PnL %
─────────────────────────────────────────────────────────────────────
Futurist        price_prediction        45   66.7% 71.4%    +2.34%
Trendy          trend_reversal          32   81.3% 65.6%    +1.87%
Pattern         pattern_match           28   50.0% 58.3%    +0.91%
```

This tells you:
- **Futurist** fired 45 signals, team executed 66.7% of them, 71.4% were winners, avg +2.34% per win
- **Trendy** has highest execution rate (81.3%) and decent win rate (65.6%)
- **Pattern** has lower execution rate (50%) — maybe team doesn't trust it yet

## Agent Output Format (Pydantic Models)

### FuturistPrediction

```python
from models.agent_outputs import FuturistPrediction

prediction = FuturistPrediction(
    predicted_price=25.50,           # What price do you predict?
    confidence=0.78,                 # How sure? 0.0-1.0
    horizon="1h",                    # 1h / 4h / 1d / 1w
    bias="BULLISH",                  # BULLISH / BEARISH / NEUTRAL / HOLD
    reasoning="RSI oversold, volume spike on dip",
    signal_type="price_prediction",  # (auto-set)
    stop_loss=22.80,                 # If it goes here, you're wrong
    take_profit=25.50,               # Target if right
)
```

### TraderDecision

```python
from models.agent_outputs import TraderDecision
from models.enums import TradeAction

decision = TraderDecision(
    action=TradeAction.BUY,          # BUY / SELL / HOLD
    entry_price=23.45,
    quantity=100,
    stop_loss=22.80,
    take_profit=25.50,
    confidence=0.78,
    reasoning="...",
    signal_type="trade_signal",      # (auto-set)
    severity="HIGH",                 # HIGH / MEDIUM / LOW
)
```

### SynthesisBrief

```python
from models.agent_outputs import SynthesisBrief

brief = SynthesisBrief(
    price=23.50,
    data_quality="good",
    news_sentiment=0.45,             # -1.0 (bearish) to +1.0 (bullish)
    pattern_type="ascending_triangle",
    trend_direction="UP",
    trend_strength=0.72,
    prediction_bias="BULLISH",
    prediction_confidence=0.65,
    structural_status="GREEN",
    consensus="BULLISH",
    consensus_pct=0.73,
    signal_type="synthesis_consensus",  # (auto-set)
    confidence=0.73,                    # Overall confidence
)
```

## Using SignalManager in Code

```python
from signal_manager import SignalManager

manager = SignalManager(DB_PATH)

# Log an alert
alert_id = manager.log_alert(
    agent_name="Futurist",
    signal_type="price_prediction",
    confidence=0.78,
    severity="MEDIUM",
    entry_price=23.45,
    stop_loss=22.80,
    take_profit=25.50,
    reasoning="...",
)

# Log feedback after team executes
manager.log_feedback(
    alert_id=alert_id,
    action_taken="executed",
    entry_price=23.45,
    exit_price=24.50,
    quantity=100,
    team_member="Alice",
    team_notes="Strong support at 23.40",
)

# Get metrics (win rate, execution rate, etc.)
metrics = manager.get_signal_metrics(agent_name="Futurist", days=30)
for m in metrics["metrics"]:
    print(f"{m['agent']}: {m['win_rate']:.0%} win rate, {m['execution_rate']:.0%} executed")

# Get recent alerts
alerts = manager.get_recent_alerts(limit=10, agent_name="Futurist")

# Get alert + feedback together
alert_with_feedback = manager.get_alert_with_feedback(alert_id)
print(f"Alert: {alert_with_feedback['signal_type']}, Confidence: {alert_with_feedback['confidence']:.0%}")
print(f"Feedback: {alert_with_feedback['feedback']}")
```

## Telegram Alert Example

**What the team sees:**

```
⚡ SIGNAL ALERT — Futurist

PRICE PREDICTION
Confidence: 78% 🟡 MEDIUM

📊 Setup
  Entry: $23.45
  Stop: $22.80 (2.8%)
  Target: $25.50 (8.7%)
  R/R Ratio: 1:3.11

Reasoning: RSI oversold + volume spike on dip

2026-04-22 14:23:15 ET
Alert ID: abc-123-de
```

This gives your team:
- **Confidence score** → Know which alerts to prioritize
- **Exact entry/stop/target** → No guesswork, execute immediately
- **Risk/reward ratio** → Quickly assess if it fits your risk tolerance
- **Reasoning** → Understand *why* the agent fired this signal

## Integration Checklist

- [ ] Run migration: `alembic upgrade head`
- [ ] Update Pydantic models (done — in `models/agent_outputs.py`)
- [ ] Pick 1 agent (e.g., Futurist) to wire up
- [ ] Parse agent output → FuturistPrediction Pydantic model
- [ ] Call `manager.log_alert()` + `notify_signal_alert()`
- [ ] Test: send a manual signal, team logs feedback via CLI
- [ ] Review metrics: `python log_signal_feedback.py metrics`
- [ ] Iterate: improve agent reasoning based on win rates
- [ ] Roll out to other agents (Trendy, Pattern, Synthesis, etc.)

## Next Steps

1. **Wire up Futurist** — It's the clearest signal (price prediction)
2. **Team uses CLI daily** — Log decisions as they execute
3. **Review metrics weekly** — Which agents are winning? Which need tuning?
4. **Feedback loop closes** — Agents see their win rates, adjust reasoning
5. **Automate backtest** — `backtester.py` + signal annotations → proof that signals work

## Questions?

See [CLAUDE.md](./CLAUDE.md) for system architecture or `notifier.py` for Telegram setup.
