# Integrating Episodic Memory into GME Tasks

## Quick Start (3 steps)

### 1. Add logging import to `run_single_agent.py`

At the top of the file, after existing imports:

```python
from episodic_integration import (
    log_futurist_prediction,
    log_manager_trade,
    log_synthesis_brief,
)
```

### 2. Add logging after crew execution

After `result = crew.kickoff()`:

```python
crew = Crew(agents=all_agents, tasks=all_tasks, process=Process.sequential, verbose=True)
result = crew.kickoff()

# Log the result to episodic memory
if name == "futurist":
    log_futurist_prediction(str(result))
elif name == "manager":
    log_manager_trade(str(result))
elif name == "synthesis":
    log_synthesis_brief(str(result))
```

### 3. Setup nightly discovery

```bash
# Edit crontab:
crontab -e

# Add this line (runs at 3 AM):
0 3 * * * cd /Users/user/my-agent-team && python3 .agent/auto_dream.py >> .agent/memory/dream.log 2>&1
```

---

## What's Being Logged

### Predictions (Futurist)
```
predicted_price: 24.50
confidence: 0.68
bias: "BUY"
horizon: "1h"
reasoning: "Triangle support holding, volume 2.3x avg"
```

### Trades (Manager/Trader)
```
action: "BUY"
entry_price: 24.50
quantity: 1.2
stop_loss: 23.44
take_profit: 25.98
confidence: 0.68
```

### Synthesis (Team Consensus)
```
price: 24.28
consensus: "BULLISH"
consensus_pct: 0.65
data_quality: "clean"
news_sentiment: 0.45
pattern: "symmetrical_triangle"
structural_status: "GREEN"
```

---

## Workflow for You

### Every morning:
```bash
python3 .agent/list_candidates.py
```

### When you see a good pattern:
```bash
python3 .agent/graduate.py prediction_BULLISH_68%_1h \
  --rationale "validated over 20 trials, consistently accurate"
```

### When you see a bad pattern:
```bash
python3 .agent/reject.py prediction_SELL_45%_4h \
  --reason "too specific to yesterday's market structure, won't generalize"
```

### View all lessons:
```bash
python3 .agent/recall.py --show-all
```

---

## What Happens Next

1. **Day 1-5**: Predictions & trades log to episodic memory
2. **Night 5**: `auto_dream.py` clusters patterns automatically
3. **Morning 6**: You review staged patterns with `list_candidates.py`
4. **Morning 6-7**: You graduate validated patterns
5. **Day 8+**: Agents load relevant lessons when they run

Example lesson that emerges:
```
📌 VALIDATED PATTERN:
   When Futurist predicts BULLISH with >70% confidence for 1h → 85% accuracy
   Confidence: 85% | Evidence: 24 samples
```

---

## Files to Edit

| File | Change | Purpose |
|------|--------|---------|
| `run_single_agent.py` | Add logging after `crew.kickoff()` | Capture predictions & trades |
| `crontab -e` | Add auto_dream.py to nightly cron | Auto-discover patterns |
| (read-only) | Run `list_candidates.py` daily | Review patterns |
| (read-only) | Run `graduate.py` weekly | Accept patterns |

---

## Logging Details

All logs go to `.agent/memory/episodic/`:

```
.agent/memory/
├── episodic/
│   ├── episodes.jsonl           # All events (append-only)
│   ├── prediction_*.json        # Individual predictions
│   ├── trade_*.json             # Individual trades
│   └── synthesis_*.json         # Team consensus
├── semantic/
│   └── lessons.jsonl            # Graduated patterns (source of truth)
└── candidates/
    └── candidates.jsonl         # Patterns awaiting review
```

---

## Testing

Quick test (no cron needed):

```bash
# 1. Log a fake prediction
python3 -c "
from gme_trading_system.episodic_integration import log_futurist_prediction
log_futurist_prediction('{\"bias\": \"BUY\", \"overall_confidence\": 0.75}')
"

# 2. Check it was logged
ls -la .agent/memory/episodic/

# 3. Run discovery
python3 .agent/auto_dream.py

# 4. Review
python3 .agent/list_candidates.py
```

---

## Next Steps

1. ✅ Integration complete (you are here)
2. ⬜ Setup cron: `0 3 * * * cd /path && python3 .agent/auto_dream.py >> .agent/memory/dream.log 2>&1`
3. ⬜ Run agents for a week to collect baseline data
4. ⬜ Review first batch of patterns tomorrow morning
5. ⬜ Graduate validated patterns
6. ⬜ Watch as agents auto-improve with lessons
