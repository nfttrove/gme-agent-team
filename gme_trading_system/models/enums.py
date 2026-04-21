"""Business model enums for GME trading system."""
from enum import Enum


class TeamName(str, Enum):
    """Team categories in the organization."""
    RESEARCH = "research"
    TRADING = "trading"
    RISK = "risk"
    MONITORING = "monitoring"


class AgentName(str, Enum):
    """All agent names in the system."""
    VALERIE = "Valerie"
    CHATTY = "Chatty"
    NEWSIE = "Newsie"
    PATTERN = "Pattern"
    TRENDY = "Trendy"
    FUTURIST = "Futurist"
    BOSS = "Boss"
    CTO = "CTO"
    SOCIAL = "Social"
    SYNTHESIS = "Synthesis"
    GEORISK = "GeoRisk"
    MEMORIA = "Memoria"
    BRIEFING = "Briefing"


class TaskType(str, Enum):
    """Types of tasks agents perform."""
    VALIDATION = "validation"
    COMMENTARY = "commentary"
    NEWS = "news"
    PATTERN = "pattern"
    TREND = "daily_trend"
    PREDICTION = "prediction"
    DECISION = "decision"
    STRUCTURAL = "structural_brief"
    SOCIAL = "social"
    SYNTHESIS = "synthesis"
    GEORISK = "georisk"
    RESEARCH = "research"
    TRADING = "trading"
    MONITORING = "monitoring"


class AgentStatus(str, Enum):
    """Status of an agent run."""
    OK = "ok"
    ERROR = "error"
    RUNNING = "running"
    PENDING = "pending"
    BLOCKED = "blocked"


class TradeStatus(str, Enum):
    """Status of a trade through its lifecycle."""
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TradeAction(str, Enum):
    """Trade action type."""
    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"
    COVER = "COVER"
    HOLD = "HOLD"
