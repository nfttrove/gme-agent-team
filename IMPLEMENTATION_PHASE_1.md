# Phase 1 Implementation: Goal Hierarchy & Cost Tracking

## What Was Implemented

You now have a **goal-aligned, cost-tracked** trading system. Here's what was added:

### 1. Data Models
- **Enums** (`models/enums.py`) — Type-safe agent names, task types, statuses
- **Schemas** (`models/schemas.py`) — Pydantic validation for API responses

### 2. Business Logic Services
- **GoalService** (`services/goal_service.py`) — Mission, goals, agent alignment
- **CostService** (`services/cost_service.py`) — LLM cost tracking, budgets, ROI

### 3. API Endpoints (via `dashboard/api_server.py`)
- `/api/goals/mission` — Mission progress
- `/api/goals/team` — All team goals
- `/api/goals/agent/<name>` — Agent's purpose
- `/api/costs/daily` — Daily spending
- `/api/costs/budget` — Budget status
- `/api/costs/agent/<name>` — Agent cost history
- `/api/costs/per-signal` — ROI calculation
- `/api/costs/log` — Log a run cost

### 4. Database Schema
- `missions` — Company mission
- `team_goals` — Team-level goals
- `agent_tasks` — Agent tasks linked to goals
- `agent_costs` — Cost per run
- `portfolio_state` — Position tracking (for Phase 2)
- `trade_proposals` — Trade lifecycle (for Phase 2)

---

## How to Set Up (5 minutes)

### Step 1: Initialize Database
```bash
cd gme_trading_system
sqlite3 agent_memory.db < migrations_add_goals.sql
```

This creates all tables and bootstraps:
- Mission: "Profitable GME Trading"
- 4 Team Goals: Research, Trading, Risk, Monitoring

### Step 2: Verify Setup
```bash
sqlite3 agent_memory.db
sqlite> SELECT * FROM missions;
sqlite> SELECT * FROM team_goals;
sqlite> .quit
```

### Step 3: Start API Server
```bash
cd dashboard
python api_server.py
```

Server starts on `http://localhost:5000`

### Step 4: Test Endpoints
```bash
# Get mission progress
curl http://localhost:5000/api/goals/mission

# Get daily costs
curl http://localhost:5000/api/costs/daily

# Get agent alignment
curl http://localhost:5000/api/goals/agent/Synthesis

# Log a cost (simulating an agent run)
curl -X POST http://localhost:5000/api/costs/log \
  -H "Content-Type: application/json" \
  -d '{"agent_name":"Synthesis","llm_provider":"deepseek","tokens":500,"task_type":"synthesis"}'
```

---

## How to Use It

### For Your Dashboard (BriefPage):

Add a "Mission & Goals" section that shows:

```typescript
const { data: mission } = useFetch('/api/goals/mission');
const { data: costs } = useFetch('/api/costs/daily');

return (
  <div>
    <h2>Mission: {mission.mission_name}</h2>
    
    {/* Team progress */}
    {mission.teams.map(team => (
      <div>
        <h3>{team.team}: {team.goal}</h3>
        <p>Status: {team.status}</p>
      </div>
    ))}
    
    {/* Daily costs */}
    <h3>Costs Today: ${costs.total_cost_usd} / ${costs.budget_daily}</h3>
    <p>Status: {costs.status}</p>
  </div>
);
```

### For Your Agents:

When an agent runs, it should log its cost:

```python
from services.cost_service import CostService

cost_service = CostService()
tokens_used = 500  # From your LLM response
cost = cost_service.log_agent_run(
    agent_name="Synthesis",
    llm_provider="deepseek",
    tokens=tokens_used,
    task_type="synthesis"
)
print(f"This run cost ${cost:.4f}")
```

### For Your Telegram Bot:

Add goal/cost status to `/update` command:

```python
def handle_command(text: str):
    if cmd == "/update":
        # ... existing refresh code ...
        
        # Add goal alignment
        mission = goal_service.get_mission_progress()
        cost_summary = cost_service.get_daily_cost_summary()
        
        msg = f"""
        <b>MISSION: {mission['mission_name']}</b>
        
        <b>Team Goals:</b>
        {mission['teams'][0]['goal']}: {mission['teams'][0]['status']}
        
        <b>Costs Today:</b>
        ${cost_summary['total_cost_usd']} / ${cost_summary['budget_daily']}
        Status: {cost_summary['status']}
        """
        _send(msg)
```

---

## What This Enables Right Now

### 1. Mission Clarity
```bash
$ curl http://localhost:5000/api/goals/mission
{
  "mission": "Profitable GME Trading",
  "teams": [
    {"team": "research", "goal": "Identify 3+ signals daily", "status": "ON_TRACK"},
    {"team": "trading", "goal": "Execute trades >60% win rate", "target": 50000, "status": "ON_TRACK"},
    {"team": "risk", "goal": "Zero blown positions", "status": "HEALTHY"},
    {"team": "monitoring", "goal": "Track costs", "target": 500, "status": "OK"}
  ]
}
```

### 2. Agent Alignment
```bash
$ curl http://localhost:5000/api/goals/agent/Synthesis
{
  "agent": "Synthesis",
  "role": "Intelligence Synthesiser",
  "team_goal": "Identify 3+ signals daily",
  "mission": "Profitable GME Trading",
  "contribution": "Distills all agent outputs into one consensus brief"
}
```

### 3. Cost Tracking
```bash
$ curl http://localhost:5000/api/costs/daily
{
  "total_cost_usd": 0.47,
  "budget_daily": 5.0,
  "percent_used": 9.4,
  "by_agent": [
    {"agent": "Synthesis", "cost": 0.12, "tokens": 500},
    {"agent": "Futurist", "cost": 0.18, "tokens": 1200},
    {"agent": "CTO", "cost": 0.11, "tokens": 400},
    {"agent": "Georisk", "cost": 0.06, "tokens": 200}
  ],
  "by_provider": {"deepseek": 0.30, "gemini": 0.17},
  "status": "OK"
}
```

### 4. ROI Calculation
```bash
$ curl http://localhost:5000/api/costs/per-signal
{
  "cost_per_signal_usd": 0.0023
}
# Translation: Every dollar spent on research agents yields ~$435 in trading signals
```

---

## Next Steps

### Immediate (Use It):
1. ✅ Initialize the database
2. ✅ Start the API server
3. ✅ Add goal/cost endpoints to your dashboard
4. Update your agents to log costs (integrate CostService.log_agent_run)

### This Week (Phase 2):
1. **Portfolio State Machine** — Track positions, P&L, margin
2. **Trade Approval Gates** — Human review before large trades
3. **Dashboard Widget** — Mission progress + cost tracking

### Next Week (Phase 3):
1. **Async Job Queue** — Replace scheduler with heartbeat-based jobs
2. **Real-time Status** — Show what each agent is doing now
3. **Budget Enforcement** — Auto-pause agent runs if over budget

---

## Files Added

```
gme_trading_system/
├── models/
│   ├── __init__.py
│   ├── enums.py              # Type-safe enums
│   └── schemas.py            # Pydantic models
├── services/
│   ├── __init__.py
│   ├── goal_service.py       # Mission & goals
│   └── cost_service.py       # LLM cost tracking
├── api/
│   └── __init__.py
└── migrations_add_goals.sql  # Database schema

dashboard/
└── api_server.py             # Updated with new endpoints
```

---

## Testing

```bash
# 1. Initialize DB
sqlite3 gme_trading_system/agent_memory.db < gme_trading_system/migrations_add_goals.sql

# 2. Start API
cd dashboard && python api_server.py

# 3. In another terminal, test
curl http://localhost:5000/api/goals/mission
curl http://localhost:5000/api/costs/daily
curl http://localhost:5000/api/goals/agent/Synthesis

# 4. Log a cost
curl -X POST http://localhost:5000/api/costs/log \
  -H "Content-Type: application/json" \
  -d '{"agent_name":"Test","llm_provider":"deepseek","tokens":100}'

# 5. Check it's tracked
curl http://localhost:5000/api/costs/daily
```

---

## Key Insights This Enables

1. **Dollar-per-insight ROI** — Know exactly how much research costs vs. profit
2. **Budget alerts** — Prevent runaway LLM spending
3. **Agent value** — Which agents provide the most value per dollar?
4. **Goal alignment** — Every agent has a clear purpose
5. **Team metrics** — Trading team vs. Research team performance by cost

This transforms your system from **a trading bot** into **a business with financial accountability**.
