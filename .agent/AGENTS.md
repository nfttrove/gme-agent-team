# Trading Agent Team

## Overview
CrewAI agents for autonomous GME trading with continual learning.

## Agents
- **Analyst**: Extract key metrics from market data (price, volume, IV, Greeks)
- **Strategist**: Propose trades based on analysis + recalled lessons
- **Manager**: Coordinate team, validate trades against hard rules

## Memory Integration
Before each cycle, agents recall relevant lessons:
```
python3 .agent/tools/recall.py "GME market conditions, recent volatility, IV strategy"
```

Lessons inform decision-making without explicit instructions.

## Learning Loop
1. **Episodic**: Trade outcomes logged to `memory/episodic/trades.jsonl`
2. **Dream**: Nightly `auto_dream.py` clusters patterns → candidates
3. **Review**: You graduate lessons daily via `graduate.py`
4. **Recall**: Future cycles load graduated lessons automatically

## Commands (via Telegram)
- `/learn "<rule>" --why "<rationale>"` — teach a lesson immediately
- `/lessons` — show current learned rules
- `/recall <intent>` — surface relevant lessons
- `/show` — brain state dashboard
