"""Pydantic models for validated agent outputs.

Agents emit free-form text; the extractors in episodic_integration parse that
text into dicts. These models validate those dicts before they land in the
episodic log, so a malformed output never corrupts the memory store.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
from .enums import TradeAction


Bias = Literal["BULLISH", "BEARISH", "NEUTRAL", "HOLD"]
Consensus = Literal["BULLISH", "BEARISH", "NEUTRAL"]
StructuralStatus = Literal["GREEN", "YELLOW", "RED"]
TrendDirection = Literal["UP", "DOWN", "SIDEWAYS", "up", "down", "sideways"]


class FuturistPrediction(BaseModel):
    """A price prediction from the Futurist agent."""
    predicted_price: float = Field(..., gt=0, description="Predicted price in USD")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0-1.0 confidence in prediction")
    horizon: str = Field(..., description="e.g. '1h', '1d', '1w'")
    bias: Bias = "HOLD"
    reasoning: str = ""
    signal_type: str = "price_prediction"
    stop_loss: Optional[float] = Field(None, description="Suggested stop loss price")
    take_profit: Optional[float] = Field(None, description="Suggested take profit price")

    @field_validator("horizon")
    @classmethod
    def _valid_horizon(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or v[-1] not in {"m", "h", "d", "w"}:
            raise ValueError(f"horizon must end with m/h/d/w, got {v!r}")
        return v


class TraderDecision(BaseModel):
    """A trade decision from the Manager/Trader agent."""
    action: TradeAction
    entry_price: float = Field(..., gt=0)
    quantity: float = Field(..., ge=0)
    stop_loss: float = Field(..., ge=0)
    take_profit: float = Field(..., ge=0)
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0-1.0 confidence in trade")
    reasoning: str = ""
    signal_type: str = "trade_signal"
    severity: str = Field("MEDIUM", description="HIGH, MEDIUM, or LOW urgency")

    @field_validator("stop_loss")
    @classmethod
    def _stop_below_entry_for_long(cls, v: float, info) -> float:
        # Only validate when we have the values we need
        action = info.data.get("action")
        entry = info.data.get("entry_price")
        if action == TradeAction.BUY and entry is not None and v > 0 and v >= entry:
            raise ValueError(f"BUY stop_loss ({v}) must be below entry_price ({entry})")
        if action == TradeAction.SHORT and entry is not None and v > 0 and v <= entry:
            raise ValueError(f"SHORT stop_loss ({v}) must be above entry_price ({entry})")
        return v


class SynthesisBrief(BaseModel):
    """Consensus brief from the Synthesis agent."""
    price: float = Field(..., ge=0)
    data_quality: str = "unknown"
    news_sentiment: float = Field(0.0, ge=-1.0, le=1.0)
    pattern_type: str = "none"
    trend_direction: TrendDirection = "sideways"
    trend_strength: float = Field(0.0, ge=0.0, le=1.0)
    prediction_bias: Bias = "HOLD"
    prediction_confidence: float = Field(0.5, ge=0.0, le=1.0)
    structural_status: StructuralStatus = "YELLOW"
    consensus: Consensus = "NEUTRAL"
    consensus_pct: float = Field(0.5, ge=0.0, le=1.0)
    signal_type: str = "synthesis_consensus"
    confidence: float = Field(0.5, ge=0.0, le=1.0, description="Overall confidence in synthesis")


class NewsSignal(BaseModel):
    """A single news item scored for relevance and sentiment."""
    headline: str = Field(..., min_length=1)
    source: Optional[str] = None
    sentiment_score: float = Field(..., ge=-1.0, le=1.0)
    sentiment_label: Optional[Literal["positive", "negative", "neutral"]] = None
    relevance_score: float = Field(0.0, ge=0.0, le=1.0)
    summary: Optional[str] = None
    signal_type: str = "sentiment_signal"
    confidence: float = Field(0.5, ge=0.0, le=1.0, description="Confidence in signal")


class TrendySignal(BaseModel):
    """Daily trend signal from Trendy agent."""
    trend_direction: Literal["UP", "DOWN", "SIDEWAYS"]
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in trend")
    support_level: float = Field(..., gt=0, description="Support price level")
    resistance_level: float = Field(..., gt=0, description="Resistance price level")
    reasoning: str = ""
    signal_type: str = "trend_signal"
    severity: str = Field("MEDIUM", description="HIGH, MEDIUM, or LOW urgency")


class PatternSignal(BaseModel):
    """Chart pattern signal from Pattern agent."""
    pattern_type: str = Field(..., description="e.g. 'ascending_triangle', 'breakout', 'flag'")
    confidence: float = Field(..., ge=0.0, le=1.0)
    breakout_level: float = Field(..., gt=0, description="Price level where pattern breaks")
    breakout_direction: Literal["UP", "DOWN"]
    reasoning: str = ""
    signal_type: str = "pattern_signal"
    severity: str = Field("MEDIUM", description="HIGH, MEDIUM, or LOW urgency")
