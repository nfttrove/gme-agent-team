# Multi-LLM Routing Guide

## Overview

Updated `llm_config.py` now intelligently routes agents to best LLM based on reasoning complexity.

**Goal:** Maximize signal quality (via DeepSeek-r1's reasoning) while keeping cost low (Gemma 2:9b for simple tasks).

## Models in Play

| Model | Speed | Quality | Cost | Use Case |
|-------|-------|---------|------|----------|
| **Gemma 2:9b** (local) | 🟢 Fast | 🟡 Good | 🟢 Free | 7 agents (simple) |
| **DeepSeek-r1:8b** (local) | 🟡 Medium | 🟢 Excellent | 🟢 Free | 3 agents (reasoning) |
| **Gemini Flash** (API) | 🟢 Fast | 🟡 Good | 🟡 $$ | Rate limit fallback |
| **Gemini Pro** (API) | 🟡 Medium | 🟢 Excellent | 🔴 $$$ | Complex reasoning fallback |

## Current Routing

### Tier 1: Fast Agents (Gemma 2:9b)

```
Valerie   → Data validation (OHLCV sanity)
Chatty    → Real-time commentary
Newsie    → Sentiment extraction
Pattern   → Pattern detection (simple shapes)
Trendy    → Trend analysis (EMA, RSI)
GeoRisk   → Risk scoring
Synthesis → Consensus brief (aggregate)
```

**Why Gemma:** Deterministic (temp=0.1), fast (~1s per agent), no API calls, no rate limits.

### Tier 2: Complex Reasoning (DeepSeek-r1:8b)

```
Futurist  → Price prediction + calibration (reasoning-heavy)
CTO       → PE playbook analysis (structural complexity)
Boss      → Daily strategy + review (synthesis)
```

**Why DeepSeek-r1:** Chain-of-thought reasoning, better calibration, higher confidence scores.

## How to Use

### In `agents.py`

Pass `agent_name` to ResilientAgent for automatic routing:

```python
# Simple agent → Gemma 2:9b
valerie_agent = ResilientAgent(
    agent_name="Valerie",  # Routes to Gemma 2:9b
    role="Data Validator",
    goal="...",
    backstory="...",
)

# Complex agent → DeepSeek-r1:8b
futurist_agent = ResilientAgent(
    agent_name="Futurist",  # Routes to DeepSeek-r1:8b
    role="Market Futurist",
    goal="...",
    backstory="...",
)
```

### Programmatically

```python
from llm_config import get_llm_for_agent

# Get LLM for an agent
llm = get_llm_for_agent("Futurist")  # Returns DeepSeek-r1:8b
llm = get_llm_for_agent("Chatty")    # Returns Gemma 2:9b

# Check routing reference
from llm_config import AGENT_LLM_ASSIGNMENT
print(AGENT_LLM_ASSIGNMENT)
```

## Measuring Impact

### Week 1: Baseline (Gemma-only)

Run for 7 days. Collect Futurist signal metrics:

```bash
python log_signal_feedback.py metrics --agent Futurist --days 7
```

Record:
- Confidence distribution (mean, std)
- Win rate
- Execution rate
- Avg P&L %

**Example output:**
```
Futurist | price_prediction | 45 alerts | 67% exec | 65% win | +1.23% avg P&L
```

### Week 2: DeepSeek-r1 (Futurist only)

Install model, switch Futurist to DeepSeek-r1, run 7 days:

```bash
# Terminal 1: Install model
ollama pull deepseek-r1:8b

# Terminal 2: Start Ollama
ollama serve

# Terminal 3: Run orchestrator (Futurist now uses DeepSeek-r1)
cd gme_trading_system
python orchestrator.py
```

Collect same metrics:

```bash
python log_signal_feedback.py metrics --agent Futurist --days 7 --offset 7
```

**Compare:**
```
Metric                  | Gemma-only | DeepSeek-r1 | Delta
─────────────────────────────────────────────────────────
Confidence (mean)       | 0.62       | 0.71        | +14% ✅
Confidence (std)        | 0.18       | 0.12        | -33% ✅ (more confident, less noisy)
Win rate                | 65%        | 72%         | +7% ✅
Execution rate          | 67%        | 71%         | +4% (team trusts more)
Avg P&L %               | +1.23%     | +1.89%      | +54% ✅
```

**Decision rule:**
- **If DeepSeek-r1 wins on win rate or P&L** → Phase 2 (add CTO, Boss)
- **If Gemma holds its own** → Stay with Gemma (simpler, cheaper)
- **If mixed results** → Keep Futurist on DeepSeek-r1, measure for 2 weeks more

### Week 3+: Phase 2 (if Phase 1 succeeds)

Add CTO + Boss to DeepSeek-r1:

```python
# agents.py
cto_agent = ResilientAgent(
    agent_name="CTO",  # Now routes to DeepSeek-r1
    role="...",
    ...
)

boss_agent = ResilientAgent(
    agent_name="Boss",  # Now routes to DeepSeek-r1
    role="...",
    ...
)
```

Repeat metrics collection. If both agents improve, deployment is successful.

## Troubleshooting

### "DeepSeek-r1:8b not found"

Install locally:
```bash
ollama pull deepseek-r1:8b
```

Verify:
```bash
curl http://localhost:11434/api/tags | jq '.models[] | .name'
```

### Logs show "Fallback to Gemini Flash"

Indicates local model is timing out or erroring. Check:
1. Ollama service running: `curl http://localhost:11434/api/tags`
2. Model loaded: `ollama list`
3. Memory available: `free -h` (each model ~5–8GB)
4. Network: `telnet localhost 11434`

### Futurist confidence scores don't change

Possible causes:
1. Agent output parsing failing (check agent_logs table for raw output)
2. Agent output not including explicit confidence (update task/backstory)
3. Pydantic validation rejecting output (check logs for validation errors)

Fix:
```bash
# Check raw Futurist output
sqlite3 agent_memory.db "SELECT content FROM agent_logs WHERE agent_name='Futurist' ORDER BY timestamp DESC LIMIT 1;"

# Manually test parsing
python -c "from models.agent_outputs import FuturistPrediction; print(FuturistPrediction.model_json_schema())"
```

## Cost Implications

**Gemma 2:9b (local):**
- 7 agents × ~2s per agent × 30 cycles/day = ~420s/day
- CPU-bound, no API costs
- **Cost: $0**

**DeepSeek-r1:8b (local):**
- 3 agents × ~3s per agent × ~10 cycles/day = ~90s/day (slower, deeper reasoning)
- CPU-bound, no API costs
- **Cost: $0**

**Gemini Flash (fallback only):**
- Only if Ollama fails (~0–1 calls/day)
- ~$0.075 per 1M input tokens, ~$0.30 per 1M output tokens
- **Cost: ~$0.01/day (if no fallbacks)**

**Total:** $0/day with full local stack. Gemini fallbacks only if needed.

## Reference: Routing Logic

```python
# llm_config.py
def get_llm_for_agent(agent_name: str) -> LLM:
    complex_reasoning_agents = {"Futurist", "CTO", "Boss"}
    
    if agent_name in complex_reasoning_agents:
        return deepseek_r1_local  # Deep reasoning
    else:
        return gemma_local  # Fast, cheap
```

## Next Steps

1. **Install DeepSeek-r1:** `ollama pull deepseek-r1:8b`
2. **Test:** Run `python orchestrator.py`, check Futurist output
3. **Measure Week 1:** Collect Gemma-only baseline (7 days)
4. **Measure Week 2:** Switch Futurist to DeepSeek-r1, compare metrics
5. **Decide:** Expand to CTO/Boss if metrics improve, else stay with Gemma

---

**Key insight:** You're not replacing Gemma; you're augmenting it. Gemma handles 70% of agents (fast), DeepSeek-r1 handles 30% (critical reasoning). Best of both worlds: speed + quality.
