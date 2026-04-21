# Business Model Refactor: Your System vs Hedge Fund vs Paperclip

## Their Business Models

### AI Hedge Fund: Graph-Based Portfolio Workflow
```
Flow (visual graph)
  ↓
  Analysts (specialized agents)
    ├─ Warren Buffett (value)
    ├─ Peter Lynch (growth)
    └─ Michael Burry (bearish)
  ↓
  Portfolio Manager (aggregates signals)
  ↓
  Risk Manager (validates decisions)
  ↓
  Execution (buy/sell with position tracking)
```

**Key:** Structured workflow, approval gates, portfolio state machine

### Paperclip: Company Org Model
```
Company Mission: "Build #1 AI note-taking app"
  ↓
  Team Goals
    ├─ Engineering: "Ship backend API by Friday"
    ├─ Product: "Get 100 beta users"
    └─ Marketing: "5K Twitter followers"
  ↓
  Agent Tasks (assigned by their role)
    ├─ CTO: "Code review PRs" (daily)
    ├─ Engineer: "Implement auth" (one-off)
    └─ Marketer: "Tweet progress" (daily)
  ↓
  Heartbeat (agents check in, get work, report)
  ↓
  Dashboard (costs, status, progress)
```

**Key:** Goals cascade down, agents pull work, costs tracked, human approval for critical decisions

---

## Your System: What You're Missing

### Current: Ad-Hoc Agent Orchestration
```
Orchestrator
  ├─ Run Valerie (every 1 min) ← No goal, just scheduled
  ├─ Run Synthesis (every 5 min)
  ├─ Run Futurist (every 2 hrs)
  └─ Run Trader (event-driven, but no approval gate)

No:
  ❌ Mission/goals
  ❌ Risk gates (Boss can reject, but auto-executes if approved)
  ❌ Portfolio state tracking
  ❌ Cost per agent
  ❌ Human approval checkpoints
  ❌ Goal alignment (agents don't know why they exist)
```

### Better: Adopt Both Patterns

**Pattern 1: Goal Hierarchy (from Paperclip)**
```python
class CompanyMission(BaseModel):
    name: str = "Profitable GME trading"
    description: str = "Maximize risk-adjusted returns via sentiment + patterns"

class TeamGoal(BaseModel):
    mission_id: int
    team: str = "Trading"  # or "Research", "Risk"
    goal: str = "Execute profitable trades"
    quarterly_target: float = 50000  # $50K profit

class AgentTask(BaseModel):
    goal_id: int
    agent: str  # Valerie, Synthesis, Futurist, Boss, TraderJoe
    task: str = "Validate data and flag anomalies"
    schedule: str = "every 1 min"
    required_for: str = "team_goal"  # Links to goal
```

**Pattern 2: State Machine (from Hedge Fund)**
```python
class TradeState(BaseModel):
    """Current portfolio + pending decisions"""
    cash: float
    positions: Dict[str, Position]  # {"GME": {"qty": 100, "avg_cost": 22.50}}
    pending_trades: List[Trade]  # From Boss, awaiting approval
    executed_trades: List[Trade]

class Trade(BaseModel):
    """Decision → Execution"""
    signal_from: str = "Futurist"
    action: str  # "BUY" | "SELL" | "HOLD"
    price: float
    confidence: float
    status: TradeStatus  # "PENDING_APPROVAL" → "APPROVED" → "EXECUTED"
    approved_by: Optional[str] = None
    approval_reason: Optional[str] = None
```

**Pattern 3: Cost Tracking (from Paperclip)**
```python
class AgentCost(BaseModel):
    agent_name: str
    period: str = "day" | "week" | "month"
    tokens_used: int
    llm_cost: float
    budget_allocated: float
    percent_of_budget: float
    status: str  # "OK" | "WARNING" | "OVER_BUDGET"
```

---

## Refactor Plan: 3-Phase Rollout

### Phase 1: Add Goal & Cost Tracking (Today - 2 hours)
- Create mission/goal/task hierarchy in DB
- Track LLM costs per agent per run
- Add cost dashboard endpoint

### Phase 2: State Machine for Trading (Tomorrow - 4 hours)
- Portfolio state model (positions, cash, P&L)
- Trade lifecycle: analyze → propose → approve → execute
- Human approval for large trades

### Phase 3: Async Job Queue (Next day - 6 hours)
- Replace scheduler with job queue (APScheduler or Celery)
- Agents report heartbeat
- Dashboard shows real-time status

---

## Implementation: Phase 1 (Start Now)

### 1. Add Goal Hierarchy Tables

```python
# models/enums.py
from enum import Enum

class TeamName(str, Enum):
    RESEARCH = "research"
    TRADING = "trading"
    RISK = "risk"
    MONITORING = "monitoring"

# models/schemas.py
from pydantic import BaseModel
from datetime import datetime

class MissionResponse(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class TeamGoalResponse(BaseModel):
    id: int
    mission_id: int
    team: TeamName
    goal: str
    quarterly_target: float
    created_at: datetime
    
    class Config:
        from_attributes = True

class AgentTaskResponse(BaseModel):
    id: int
    goal_id: int
    agent_name: str
    task: str
    schedule: str
    required_for: str
    created_at: datetime
    
    class Config:
        from_attributes = True
```

### 2. Add to Database Schema

```sql
-- alembic migration
CREATE TABLE missions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE team_goals (
    id SERIAL PRIMARY KEY,
    mission_id INTEGER NOT NULL REFERENCES missions(id),
    team VARCHAR(50) NOT NULL,  -- research, trading, risk, monitoring
    goal VARCHAR(500) NOT NULL,
    quarterly_target FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE agent_tasks (
    id SERIAL PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES team_goals(id),
    agent_name VARCHAR(100) NOT NULL,
    task VARCHAR(500) NOT NULL,
    schedule VARCHAR(100),  -- "every 1 min", "event-driven", etc.
    required_for VARCHAR(100),  -- Which goal this feeds
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE agent_costs (
    id SERIAL PRIMARY KEY,
    agent_name VARCHAR(100) NOT NULL,
    run_timestamp TIMESTAMP,
    llm_provider VARCHAR(50),  -- "deepseek", "gemini", "gemma"
    tokens_used INTEGER,
    cost_usd FLOAT,
    task_type VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW(),
    
    INDEX idx_agent_period (agent_name, run_timestamp)
);

CREATE TABLE portfolio_state (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP,
    cash FLOAT,
    positions JSON,  -- {"GME": {"qty": 100, "avg_cost": 22.50, "current_price": 23.00}}
    unrealized_pnl FLOAT,
    realized_pnl FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 3. Create Goal Alignment Dashboard

```python
# services/goal_service.py
class GoalService:
    def get_mission_progress(self) -> dict:
        """How close are we to quarterly targets?"""
        return {
            "mission": "Profitable GME trading",
            "teams": [
                {
                    "name": "Research",
                    "goal": "Identify 3+ strong signals daily",
                    "actual": "Yesterday: 5 signals",
                    "status": "ON_TRACK"
                },
                {
                    "name": "Trading",
                    "goal": "$50K profit this quarter",
                    "actual": "$12,500 so far (25%)",
                    "status": "ON_TRACK"
                },
                {
                    "name": "Risk",
                    "goal": "0 blown positions (hard stop)",
                    "actual": "0 blown positions",
                    "status": "HEALTHY"
                }
            ]
        }
    
    def get_agent_alignment(self, agent_name: str) -> dict:
        """Why does this agent exist?"""
        return {
            "agent": "Valerie",
            "role": "Data Validator",
            "team_goal": "Identify 3+ strong signals daily",
            "mission": "Profitable GME trading",
            "contribution": "Flags bad ticks so other agents trust the data",
            "recent_runs": [...]
        }

# services/cost_service.py
class CostService:
    def get_daily_costs(self) -> dict:
        """How much did we spend on LLMs today?"""
        return {
            "total_today": 0.47,
            "budget_daily": 5.00,
            "percent_used": 9.4,
            "by_agent": [
                {"agent": "Synthesis", "cost": 0.12, "tokens": 500},
                {"agent": "Futurist", "cost": 0.18, "tokens": 1200},
                {"agent": "CTO", "cost": 0.11, "tokens": 400},
                {"agent": "Georisk", "cost": 0.06, "tokens": 200},
            ]
        }
    
    def track_run_cost(self, agent_name: str, llm_provider: str, tokens: int):
        """Log cost of a single agent run"""
        cost = self._estimate_cost(llm_provider, tokens)
        # Save to agent_costs table
        # Check budget, warn if approaching limit

# api/routes/goals.py
@app.get("/api/goals/mission")
async def get_mission():
    return goal_service.get_mission_progress()

@app.get("/api/goals/agent/{agent_name}")
async def get_agent_alignment(agent_name: str):
    return goal_service.get_agent_alignment(agent_name)

@app.get("/api/costs/daily")
async def get_daily_costs():
    return cost_service.get_daily_costs()

@app.get("/api/costs/agent/{agent_name}")
async def get_agent_costs(agent_name: str, days: int = 7):
    return cost_service.get_agent_costs(agent_name, days)
```

### 4. Update Agent Service to Track Costs

```python
# services/agent_service.py
class AgentService:
    async def run_agent_with_tracking(self, agent_name: str) -> AgentResult:
        """Run agent and log cost + link to goal"""
        start_time = time.time()
        
        try:
            # Get which goal this agent serves
            goal = self.goal_service.get_agent_goal(agent_name)
            
            # Run the agent (this is where LLMs are called)
            result = await self._run_agent_impl(agent_name)
            
            # Track cost
            tokens_used = result.get("tokens_used", 0)
            llm_provider = result.get("llm_provider", "deepseek")
            cost = self._estimate_cost(llm_provider, tokens_used)
            
            # Save to database
            await self._log_run(
                agent_name=agent_name,
                goal_id=goal.id,
                task_type=result["task_type"],
                status="ok",
                tokens=tokens_used,
                cost=cost,
                duration_sec=time.time() - start_time
            )
            
            return result
            
        except Exception as e:
            await self._log_run(
                agent_name=agent_name,
                status="error",
                error=str(e),
                cost=0
            )
            raise
```

---

## What This Enables

### For You:
- **See why each agent exists** — "Valerie feeds Research goal"
- **Monitor spending** — "$0.47 of $5 budget used today"
- **Track progress** — "On pace for $50K profit this quarter"
- **Cost per insight** — "Synthesis costs $0.02, saved us $500 by avoiding bad trade"

### For Your Team/Investors:
- **Dashboard clarity** — "GME system is 25% toward quarterly profit target"
- **Cost visibility** — "We spend $3/day on LLMs for $500+ daily profit"
- **Goal alignment** — "Every agent is linked to a business objective"

### For the System:
- **Runaway cost prevention** — Alert if daily LLM spend > budget
- **Performance metrics** — Cost per signal, cost per trade
- **ROI calculation** — $ profit / $ LLM spend

---

## Quick Start (Next 30 min)

1. Create `models/enums.py` with TeamName enum
2. Create `models/schemas.py` with Mission/Goal/Task/Cost schemas
3. Create `services/goal_service.py` with basic methods
4. Create `api/routes/goals.py` with 4 endpoints
5. Manually insert one mission + goals into DB (bootstrap)

This gives you the **conceptual framework**. Then you can wire it into your existing agents over time.

---

## Next Phases (Future)

**Phase 2: Portfolio State Machine**
- Track positions, cash, P&L
- Trade approval gates (human can veto)
- P&L per strategy

**Phase 3: Async Execution**
- Job queue instead of scheduler
- Agents can pull work (not just scheduled)
- Better monitoring and error recovery

---

## How This Differs From Your Current System

| Aspect | Current | After Refactor |
|--------|---------|-----------------|
| **Why agents run** | Schedule | Mission → Goal → Task |
| **Cost visibility** | None | Per agent, daily, by LLM |
| **Portfolio tracking** | None | Full state machine |
| **Approval gates** | Boss approves, auto-executes | Human review before big trades |
| **ROI tracking** | Manual | Automated ($ profit / $ LLM cost) |
| **Goal alignment** | Implicit | Explicit hierarchy |

This transforms your system from **a collection of agents** to **a company with structure**.
