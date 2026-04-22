# Wire Remaining Agents to Signal Confidence System (Option C)

## Overview

Now that Futurist is wired and backtested (Options A + B), roll out to other agents.

**Pattern:** Each agent follows the same flow:
1. Agent runs (solo, one task)
2. Output parsed to appropriate Pydantic model
3. Signal logged to `signal_alerts`
4. Telegram alert sent
5. Team logs feedback via CLI

## Agents to Wire (Priority Order)

| Agent | Model | Cycle | Why First |
|-------|-------|-------|-----------|
| **Trendy** | Gemma 2:9b | Every 4h + 8 PM | Clear signals (trend = up/down/sideways) |
| **Synthesis** | Gemma 2:9b | Every 5 min | Consensus brief (easy to score) |
| **Pattern** | Gemma 2:9b | Every 2h | Pattern detection (hit/miss clear) |
| **Newsie** | Gemma 2:9b | Every 30m | Sentiment signals (bullish/bearish) |

## Template: Wire a New Agent

### Step 1: Pydantic Model (if needed)

Check if a model exists in `models/agent_outputs.py`. If not, create one:

```python
# models/agent_outputs.py
class TrendySignal(BaseModel):
    """Daily trend signal from Trendy agent."""
    trend_direction: Literal["UP", "DOWN", "SIDEWAYS"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    support_level: float
    resistance_level: float
    reasoning: str = ""
    signal_type: str = "trend_signal"
    severity: str = Field("MEDIUM", description="HIGH, MEDIUM, or LOW urgency")
```

### Step 2: Create Agent Cycle in Orchestrator

Copy the Futurist pattern and adapt:

```python
# orchestrator.py

@active_window_required
def run_trendy_signal():
    """Trendy agent trend signal with confidence logging."""
    from agents import daily_trend_agent
    from tasks import daily_trend_task
    import json
    import re

    log.info("[Trendy] Starting trend signal cycle")
    write_log("Trendy", "Running trend signal cycle", "trend_signal", "running")

    with metrics.cycle("trendy_signal"):
        try:
            crew = Crew(
                agents=[daily_trend_agent],
                tasks=[daily_trend_task],
                process=Process.sequential,
                verbose=True,
            )
            result = safe_kickoff(crew, timeout_seconds=300, label="trendy_signal")

            # Parse output to TrendySignal
            signal_data = _extract_json(str(result))  # Reuse JSON extraction
            if not signal_data:
                log.warning("[Trendy] Could not parse signal")
                return

            try:
                signal = TrendySignal(**signal_data)
            except Exception as e:
                log.error(f"[Trendy] Validation failed: {e}")
                return

            write_log("Trendy", str(result)[:1000], "trend_signal", "ok")

            # Log signal
            manager = SignalManager(DB_PATH)
            alert_id = manager.log_alert(
                agent_name="Trendy",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                severity=signal.severity,
                entry_price=signal.support_level if signal.trend_direction == "UP" else signal.resistance_level,
                stop_loss=signal.resistance_level if signal.trend_direction == "UP" else signal.support_level,
                take_profit=signal.resistance_level if signal.trend_direction == "UP" else signal.support_level * 0.95,
                reasoning=signal.reasoning,
            )

            # Send alert
            notify_signal_alert(
                agent_name="Trendy",
                signal_type=signal.signal_type,
                confidence=signal.confidence,
                entry_price=signal.support_level,
                stop_loss=signal.resistance_level,
                take_profit=signal.resistance_level * 1.02,
                reasoning=signal.reasoning,
                alert_id=alert_id,
            )

            metrics.snapshot()

        except CrewTimeout as e:
            log.error(f"[Trendy] TIMEOUT: {e}")
            write_log("Trendy", f"TIMEOUT: {e}", "trend_signal", "timeout")
        except Exception as e:
            log.error(f"[Trendy] {e}")
            write_log("Trendy", str(e), "trend_signal", "error")
```

### Step 3: Add to Schedule

In `configure_schedule()`:

```python
# Add alongside other agents
self.scheduler.add_job(run_trendy_signal, IntervalTrigger(hours=4), id="trendy_signal")
```

### Step 4: Test

Run manually:
```bash
python -c "from orchestrator import run_trendy_signal; run_trendy_signal()"
```

Check:
```bash
# See signal in DB
sqlite3 agent_memory.db "SELECT * FROM signal_alerts WHERE agent_name='Trendy' ORDER BY timestamp DESC LIMIT 1;"

# View recent alerts
python log_signal_feedback.py recent --agent Trendy
```

## Implementation Checklist

### Trendy (Trend Analysis)

- [ ] Create `TrendySignal` Pydantic model (trend_direction, confidence, support, resistance)
- [ ] Create `run_trendy_signal()` in orchestrator
- [ ] Add to schedule: `IntervalTrigger(hours=4)` + `CronTrigger(hour=20, minute=0)` for EOD
- [ ] Test: manually trigger, verify Telegram alert
- [ ] Commit

**Estimated time:** 30 min

### Synthesis (Consensus Brief)

- [ ] Update `SynthesisBrief` Pydantic model if needed (already has confidence)
- [ ] Create `run_synthesis_signal()` in orchestrator
- [ ] Add to schedule: `IntervalTrigger(minutes=5)`
- [ ] Test
- [ ] Commit

**Estimated time:** 20 min

### Pattern (Chart Patterns)

- [ ] Create `PatternSignal` Pydantic model (pattern_type, confidence, breakout_level)
- [ ] Create `run_pattern_signal()`
- [ ] Add to schedule: `IntervalTrigger(hours=2)`
- [ ] Test
- [ ] Commit

**Estimated time:** 30 min

### Newsie (Sentiment)

- [ ] Create `NewsSignal` Pydantic model (already exists mostly, enhance with confidence)
- [ ] Create `run_newsie_signal()`
- [ ] Add to schedule: `IntervalTrigger(minutes=30)`
- [ ] Test
- [ ] Commit

**Estimated time:** 25 min

**Total:** ~2–3 hours for all 4 agents

## Helper: Unified JSON Extraction

Update orchestrator to have a shared `_extract_json()`:

```python
def _extract_json(raw_output: str) -> dict:
    """Extract JSON from agent output (handles <thought> blocks, code blocks, etc.)."""
    import json
    import re

    # Handle <thought> blocks
    thought_match = re.search(r'<thought>.*?</thought>\s*(.*)', raw_output, re.DOTALL)
    if thought_match:
        raw_output = thought_match.group(1)

    # Handle markdown code blocks
    code_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_output, re.DOTALL)
    if code_match:
        json_str = code_match.group(1)
    else:
        # Extract first JSON object
        json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
        json_str = json_match.group(0) if json_match else None

    if not json_str:
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None
```

Then use `_extract_json()` in all agent cycles instead of `_extract_futurist_prediction()`.

## Integration Order

**Day 1 (Option C Part 1): Trendy + Synthesis**
- Wire both (simple, similar to Futurist)
- Test each
- Commit as one PR

**Day 2 (Option C Part 2): Pattern + Newsie**
- Wire both
- Test
- Commit

**Result:** 4 agents + 1 (Futurist) = 5 agents emitting confidence-scored signals
- 3 tier-2 complex (Gemma) + 2 tier-1 simple (Gemma)
- Team logs feedback daily
- Weekly metrics review shows which agents are winning

## Measurement After All 5 Wired

```bash
# Week 1–2 metrics
python log_signal_feedback.py metrics --days 14

# Output example:
# Futurist      | price_prediction   | 28 | 68% | 72% | +1.89%
# Trendy        | trend_signal       | 21 | 81% | 65% | +0.95%
# Synthesis     | synthesis_consensus| 70 | 45% | 58% | +0.34%
# Pattern       | pattern_match      | 14 | 50% | 71% | +1.20%
# Newsie        | sentiment_signal   | 42 | 72% | 61% | +0.78%
```

**Insights:**
- Trendy has best execution rate (81%) — team trusts it
- Futurist + Pattern have best win rates (72% + 71%)
- Synthesis fires lots but lower win rate — maybe too sensitive?
- Next: adjust thresholds based on confidence calibration

## Questions?

See:
- `SIGNAL_CONFIDENCE_GUIDE.md` — Full API
- `example_futurist_signal.py` — Working example
- `notifier.py` — Telegram formatting
- `models/agent_outputs.py` — Pydantic definitions
