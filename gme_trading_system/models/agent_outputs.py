"""Pydantic schemas for validating agent outputs before database insertion."""
from pydantic import BaseModel, Field, validator
from typing import Optional
from datetime import datetime


class AgentResult(BaseModel):
    """Base schema for all agent outputs."""

    task_type: str = Field(..., description="Type of task executed")
    status: str = Field(..., description="Status: 'ok', 'error', 'running'")
    content: str = Field(..., description="Task output/result")
    agent_name: str = Field(..., description="Name of the agent")
    timestamp: Optional[str] = Field(None, description="ISO timestamp")

    @validator('status')
    def validate_status(cls, v):
        allowed = {'ok', 'error', 'running', 'pending', 'rejected'}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}, got {v}")
        return v

    @validator('content')
    def truncate_content(cls, v):
        """Truncate content to 5000 chars to prevent DB bloat."""
        if len(v) > 5000:
            return v[:4997] + "..."
        return v

    class Config:
        extra = 'forbid'  # Reject unexpected fields


class TradeDecisionOutput(AgentResult):
    """Schema for trade decision outputs."""
    task_type: str = Field(default='trade_decision', description="Task type")


class PredictionOutput(AgentResult):
    """Schema for price prediction outputs."""
    task_type: str = Field(default='prediction', description="Task type")


class StructuralSignalOutput(AgentResult):
    """Schema for structural signal outputs."""
    task_type: str = Field(default='structural_signal', description="Task type")


class ValidationError(BaseModel):
    """Schema for validation error logging."""
    timestamp: str
    agent_name: str
    task_type: str
    original_output: str  # truncated to 500 chars
    error_message: str
    recovery_action: str  # 'sanitized', 'rejected', 'retried'
