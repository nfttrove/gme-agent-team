"""Pydantic schemas for API requests and responses."""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any
from .enums import TeamName, TaskType, AgentStatus, TradeStatus, TradeAction


# Mission & Goals

class MissionResponse(BaseModel):
    """Mission/company objective."""
    id: int
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TeamGoalResponse(BaseModel):
    """Team-level goal aligned with mission."""
    id: int
    mission_id: int
    team: TeamName
    goal: str = Field(..., min_length=1, max_length=500)
    quarterly_target: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AgentTaskResponse(BaseModel):
    """Agent task linked to a goal."""
    id: int
    goal_id: int
    agent_name: str
    task: str = Field(..., min_length=1, max_length=500)
    schedule: str  # e.g., "every 1 min", "event-driven", "daily"
    required_for: str  # Which goal
    created_at: datetime

    class Config:
        from_attributes = True


# Costs

class AgentCostResponse(BaseModel):
    """Cost of a single agent run."""
    id: int
    agent_name: str
    run_timestamp: datetime
    llm_provider: str  # "gemini", "gemma"
    tokens_used: int
    cost_usd: float
    task_type: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class DailyCostSummary(BaseModel):
    """Daily cost summary."""
    total_cost_usd: float
    budget_daily: float
    percent_used: float
    by_agent: List[Dict[str, Any]]  # [{"agent": "Synthesis", "cost": 0.12, "tokens": 500}, ...]
    by_provider: Dict[str, float]   # {"gemini": 0.17, "gemma": 0.0}


# Trading

class PortfolioPosition(BaseModel):
    """A single position in the portfolio."""
    ticker: str
    qty: float
    avg_cost: float
    current_price: float
    unrealized_pnl: Optional[float] = None

    def calculate_pnl(self) -> float:
        """Calculate unrealized P&L."""
        return (self.current_price - self.avg_cost) * self.qty


class PortfolioStateResponse(BaseModel):
    """Current portfolio state."""
    timestamp: datetime
    cash: float
    positions: Dict[str, PortfolioPosition]
    unrealized_pnl: float
    realized_pnl: float
    total_equity: float

    class Config:
        from_attributes = True


class TradeProposal(BaseModel):
    """A trade proposed by an agent."""
    id: int
    signal_from: str  # Agent name
    action: TradeAction
    ticker: str
    price: float
    quantity: float
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    status: TradeStatus
    created_at: datetime
    approved_by: Optional[str] = None
    approval_reason: Optional[str] = None
    approved_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TradeApprovalRequest(BaseModel):
    """Request to approve/reject a trade."""
    trade_id: int
    approved: bool
    reason: str = Field(..., min_length=1, max_length=500)


# Dashboard

class MissionProgress(BaseModel):
    """Progress toward mission goals."""
    mission_name: str
    teams: List[Dict[str, Any]]  # Team progress


class AgentAlignmentResponse(BaseModel):
    """How an agent aligns with company goals."""
    agent_name: str
    role: str
    team_goal: str
    mission: str
    contribution: str
    last_run: Optional[datetime] = None
    last_status: Optional[AgentStatus] = None
    total_cost_usd: Optional[float] = None
    runs_this_week: int = 0
