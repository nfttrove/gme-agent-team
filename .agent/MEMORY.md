# Episodic Memory System for GME Intelligence

This system auto-discovers trading patterns by logging agent predictions, trades, and signals—then clustering what works.

## Folder Structure

```
.agent/
├── memory/
│   ├── episodic/          # Raw event log (predictions, trades, signals)
│   │   ├── episodes.jsonl # Append-only log of all events
│   │   └── *.json         # Individual episode backups
│   ├── semantic/          # Graduated lessons (source of truth)
│   │   └── lessons.jsonl  # Validated patterns ready for use
│   ├── candidates/        # Patterns awaiting human review
│   │   └── candidates.jsonl
│   └── working/           # Temp files (nightly cycles, etc)
├── episodic_logger.py     # Log predictions/trades/signals
├── cluster_patterns.py    # Nightly pattern discovery
├── list_candidates.py     # CLI: review staged patterns
├── graduate.py            # CLI: promote pattern to lesson
├── reject.py              # CLI: discard pattern
├── recall.py              # CLI: surface relevant lessons
└── auto_dream.py          # Nightly discovery cycle
```

## Workflow

### 1. Log Events (Agents)

After each task completes, agents log outcomes:

```python
from episodic_integration import log_futurist_prediction, log_manager_trade

# In Futurist task:
episode_id = log_futurist_prediction(agent_output, horizon="1h")

# In Manager task:
trade_id = log_manager_trade(agent_output)
```

**What gets logged:**
- Predictions: price, confidence, bias, reasoning
- Trades: action, entry, SL, TP, confidence
- Signals: pattern type, strength, bullish/bearish
- Synthesis: team consensus, data quality, sentiment

### 2. Nightly Discovery (Auto-Dream)

Runs via cron:

```bash
# First time:
python3 .agent/auto_dream.py

# Setup cron:
crontab -e
0 3 * * * python3 /path/to/project/.agent/auto_dream.py >> /path/to/project/.agent/memory/dream.log 2>&1
```

**What it does:**
- Clusters prediction accuracy by bias + confidence + horizon
- Finds which signals are most reliable
- Detects multi-signal combinations that predict outcomes
- **No reasoning, no commits—safe to run unattended.**

### 3. Human Review (Morning)

```bash
# List all staged patterns:
python3 .agent/list_candidates.py

# Sample output:
# 1. [prediction_BULLISH_68%_1h]
#    When Futurist predicts BULLISH with 68% confidence for 1h → 82% accuracy (18 trials)
#    To accept:  python3 .agent/graduate.py prediction_BULLISH_68%_1h --rationale '...'
```

### 4. Graduate or Reject

```bash
# Accept a pattern:
python3 .agent/graduate.py prediction_BULLISH_68%_1h --rationale "evidence holds, validates against test set"

# Reject a pattern:
python3 .agent/reject.py prediction_BULLISH_68%_1h --reason "too specific to GME's current volatility regime"
```

**Graduation is irreversible.** It logs the reviewer's rationale so bad decisions are visible.

### 5. Agents Load Lessons Automatically

When agents are about to run, they inject relevant lessons:

```python
from recall import inject_lessons_into_prompt

# Before Futurist task:
context = inject_lessons_into_prompt("Market Futurist")
# Returns something like:
#
# ## Validated Patterns from Prior Experience
#
# 📌 VALIDATED PATTERN:
#    When Futurist predicts BULLISH with >70% confidence for 1h → 85% accuracy
#    Confidence: 85% | Evidence: 24 samples
```

**Lessons surface automatically.** No manual prompt engineering needed.

## Example: A Validated Pattern Emerges

**Day 1:** Futurist predicts BULLISH @ 24.50 (confidence 0.75) for 1h. Actual: 24.52. ✓

**Day 2:** Same. ✓

**Day 3-10:** Pattern repeats. ✓

**Night 10:** `auto_dream.py` clusters:
```
prediction_BULLISH_75%_1h: 10 trials → 90% accuracy
```

**Morning 11:** Review staged candidate, graduate it:
```bash
python3 .agent/graduate.py prediction_BULLISH_75%_1h --rationale "10-day validation, strong signal"
```

**Next run:** Futurist's task context includes:
```
📌 VALIDATED PATTERN:
   When predicting BULLISH with 75%+ confidence for 1h → 90% accuracy (10 samples)
   
Use this to calibrate confidence bands.
```

**Result:** Futurist's next 75% BULLISH call is informed by its own track record.

## CLI Tools

| Command | Purpose |
|---------|---------|
| `python3 .agent/list_candidates.py` | Show staged patterns awaiting review |
| `python3 .agent/graduate.py <ID> --rationale '...'` | Accept pattern → adds to lessons |
| `python3 .agent/reject.py <ID> --reason '...'` | Discard pattern → logs why |
| `python3 .agent/recall.py --show-all` | Display all graduated lessons |
| `python3 .agent/auto_dream.py` | Run discovery cycle manually |

## Integration Checklist

- [ ] Call `log_futurist_prediction()` after Futurist task
- [ ] Call `log_manager_trade()` after Manager task
- [ ] Call `log_synthesis_brief()` after Synthesis task
- [ ] Setup cron: `0 3 * * * python3 .agent/auto_dream.py >> .agent/memory/dream.log 2>&1`
- [ ] Review candidates tomorrow morning: `python3 .agent/list_candidates.py`
- [ ] Graduate patterns weekly: `python3 .agent/graduate.py ... --rationale '...'`

## Memory Retention

- **Episodic** (raw events): 90 days (keep everything)
- **Candidates** (staged): Until graduated or rejected
- **Semantic** (lessons): Forever (source of truth)

## See Also

- [Agentic Stack](../Downloads/agentic-stack-master/docs/architecture.md) — full architecture
- `cluster_patterns.py` — pattern discovery logic
- `episodic_logger.py` — logging API
