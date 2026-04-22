# Signal Confidence + Feedback Loop — Implementation Checklist

## What You Have (Just Built)

✅ **Database layer** — Two new tables + indexes for signals & feedback  
✅ **Pydantic models** — Standardized confidence + signal_type in agent outputs  
✅ **Signal manager** — API to log alerts & feedback, compute metrics  
✅ **CLI tool** — Team logs decisions: `log_signal_feedback.py`  
✅ **Telegram integration** — Alerts show confidence % + risk/reward setup  
✅ **Documentation** — Full guide + working example  

## Week 1: Foundation (2–3 hours)

### Day 1: Database + Testing
- [ ] Run migration: `cd gme_trading_system && python -m alembic upgrade head`
- [ ] Verify tables created: `sqlite3 agent_memory.db ".tables"` → see `signal_alerts`, `signal_feedback`
- [ ] Test signal manager: `python example_futurist_signal.py` (simulated flow)
- [ ] Verify table schema: `sqlite3 agent_memory.db ".schema signal_alerts"`

### Day 2: Wire Futurist (Simplest Agent)
- [ ] Open `orchestrator.py`, find `run_futurist_cycle()`
- [ ] Import `SignalManager` + `notify_signal_alert` + `FuturistPrediction`
- [ ] Add signal logging after agent output is captured
  - Parse agent text output → `FuturistPrediction` Pydantic model
  - Call `manager.log_alert()` to save to DB
  - Call `notify_signal_alert()` to send Telegram
- [ ] Test: manually trigger `python -c "from orchestrator import run_futurist_cycle; run_futurist_cycle()"`
- [ ] Check Telegram — should see alert with confidence % + entry/stop/target

### Day 3: Team Uses CLI
- [ ] Run `python log_signal_feedback.py recent --limit 5` → see Futurist alerts
- [ ] Pick one alert, simulate execution: `python log_signal_feedback.py log --alert-id ABC --action executed --entry 23.45 --exit 24.50 --member YOU`
- [ ] View metrics: `python log_signal_feedback.py metrics --agent Futurist`
- [ ] Verify feedback is stored

## Week 2: Wire Remaining Agents (2–3 hours)

### Wire These (by priority)

| Agent | Priority | Why | Effort |
|-------|----------|-----|--------|
| **Trendy** | 🔴 High | Daily trend + end-of-day = clearest timeframe | 30 min |
| **Synthesis** | 🔴 High | Cross-agent consensus = highest conviction | 30 min |
| **Pattern** | 🟡 Medium | Pattern detection = more subjective | 45 min |
| **Newsie** | 🟡 Medium | Sentiment = external data, easier to backtest | 45 min |
| **CTO** | 🟢 Low | Structural signals = complex, lower frequency | 1h |

**Process for each agent:**
1. Find agent in `orchestrator.py` (e.g., `run_trendy_cycle()`)
2. Import Pydantic model (e.g., `SynthesisBrief`, create `TrendySignal` if needed)
3. Parse agent output → Pydantic
4. Log via `manager.log_alert()`
5. Notify via Telegram
6. Test: trigger cycle, verify Telegram + DB
7. Commit

**Estimate:** 2 hours for all 5 agents if you batch the changes.

## Week 3: Feedback Loop + Learning (1–2 hours)

### Team Integration
- [ ] Share CLI with team: `python log_signal_feedback.py --help`
- [ ] Create Slack/Discord snippet with common commands:
  ```bash
  # Log execution (replace alert-id, prices, name)
  python log_signal_feedback.py log --alert-id XXX --action executed --entry 23.45 --exit 24.50 --member "Your Name"

  # View metrics
  python log_signal_feedback.py metrics --days 7
  ```
- [ ] Team logs decisions for 1 week
- [ ] Review metrics: which agents are winning?

### Backtest Validation (Optional but Recommended)
- [ ] Update `backtester.py` to annotate historical signals (which would have fired?)
- [ ] Cross-reference: Futurist prediction vs. actual close → calibration error
- [ ] Prove: "Futurist 78% confidence signals have 71% historical win rate"

## Week 4: Continuous Improvement (Ongoing)

### Weekly (5 min)
- [ ] Run: `python log_signal_feedback.py metrics --days 7`
- [ ] Identify best-performing signal type
- [ ] Identify worst (lowest execution rate or win rate)
- [ ] Share insights with team: "Hey, Trendy end-of-day signals are 80% accurate — prioritize those"

### Monthly (30 min)
- [ ] Review all signal metrics
- [ ] Identify which agent confidence thresholds are reliable:
  - "Futurist 80%+ confidence: 75% win rate"
  - "Pattern < 60% confidence: 35% win rate — suppress these"
- [ ] Provide feedback to agents via `episodic_integration.py` hooks
  - Log: "Confidence >= 0.75 is reliable for Futurist. Tighten filters below that."
  - Next cycle: Futurist sees lesson, adjusts reasoning

### As Needed
- [ ] Add new signal types (e.g., "volatility_breakout") as agents evolve
- [ ] Update Pydantic models with new signal structures
- [ ] Wire new agents into feedback loop

## File Reference

### New Files (Created for You)

```
gme_trading_system/
├── alembic/versions/
│   └── 2026_04_22_add_signal_confidence_feedback.py  [Migration]
├── signal_manager.py                                  [Core: log alerts + feedback]
├── log_signal_feedback.py                             [CLI: team tool]
├── example_futurist_signal.py                         [Working example]
├── SIGNAL_CONFIDENCE_GUIDE.md                         [Full documentation]
└── SIGNAL_IMPLEMENTATION_CHECKLIST.md                 [This file]
```

### Modified Files

```
gme_trading_system/
├── models/agent_outputs.py                           [Added confidence + signal_type]
└── notifier.py                                        [Added notify_signal_alert()]
```

## Command Reference

### Database Setup
```bash
# Run migration (creates tables)
python -m alembic upgrade head

# Inspect schema
sqlite3 agent_memory.db ".schema signal_alerts"
sqlite3 agent_memory.db "SELECT COUNT(*) FROM signal_alerts;"
```

### Team CLI

```bash
# Log feedback on an alert
python log_signal_feedback.py log \
  --alert-id ABC-123-DEF \
  --action executed \
  --entry 23.45 \
  --exit 24.50 \
  --member "Alice" \
  --notes "Strong volume"

# View recent alerts
python log_signal_feedback.py recent --limit 20
python log_signal_feedback.py recent --agent Futurist

# View metrics
python log_signal_feedback.py metrics                    # All (30 days)
python log_signal_feedback.py metrics --agent Futurist  # By agent
python log_signal_feedback.py metrics --days 7          # Last 7 days
python log_signal_feedback.py metrics --signal-type price_prediction
```

### Python API (In Your Code)

```python
from signal_manager import SignalManager

manager = SignalManager(DB_PATH)

# Log a signal
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

# Log team feedback
manager.log_feedback(
    alert_id=alert_id,
    action_taken="executed",
    entry_price=23.45,
    exit_price=24.50,
    quantity=100,
    team_member="Alice",
    team_notes="...",
)

# Get metrics
metrics = manager.get_signal_metrics(agent_name="Futurist", days=30)
for m in metrics["metrics"]:
    print(f"{m['agent']}: {m['win_rate']:.0%} win, {m['execution_rate']:.0%} executed")

# Get recent alerts
alerts = manager.get_recent_alerts(limit=10)

# Get alert + feedback
alert_with_feedback = manager.get_alert_with_feedback(alert_id)
```

## Troubleshooting

### "Table signal_alerts does not exist"
Migration didn't run. Check:
```bash
python -m alembic current  # Should show 2026_04_22_signal_confidence
python -m alembic upgrade head  # Run if behind
sqlite3 agent_memory.db ".tables" | grep signal
```

### Telegram alert not showing confidence
Verify:
1. `notifier.py` has `notify_signal_alert()` function
2. You're calling it (not old `notify_trade()`)
3. Telegram token is set (`.env` or env var)
4. Try: `python -c "from notifier import test_connection; test_connection()"`

### CLI shows "No recent alerts"
- Agents haven't fired yet (run a cycle manually)
- OR signals logged but with different `agent_name` (check DB: `sqlite3 agent_memory.db "SELECT DISTINCT agent_name FROM signal_alerts;"`)

### SignalManager import fails
```bash
python -c "from signal_manager import SignalManager; print('OK')"
```

If fails, ensure `signal_manager.py` is in `gme_trading_system/` and you're running from that directory.

## Quick Win: Validate Everything Works

```bash
cd gme_trading_system

# 1. Check migration
python -m alembic current

# 2. Run example
python example_futurist_signal.py

# 3. Check tables
sqlite3 agent_memory.db "SELECT COUNT(*) FROM signal_alerts;"

# 4. View CLI help
python log_signal_feedback.py metrics

# 5. All working? You're ready!
```

## Next Steps

1. **This week:** Run migration, wire Futurist, test end-to-end
2. **Next week:** Wire remaining 4 agents (Trendy, Synthesis, Pattern, Newsie)
3. **Week 3:** Team logs decisions for 7 days, review metrics
4. **Week 4+:** Continuous improvement loop — agents learn from win rates

## Questions?

- Pydantic model details: `models/agent_outputs.py`
- Full guide: `SIGNAL_CONFIDENCE_GUIDE.md`
- Working example: `example_futurist_signal.py`
- Telegram setup: `notifier.py` (top of file)

---

**You're now 1 week away from a closed feedback loop where agents improve based on real team execution data.**

Good luck! 🚀
