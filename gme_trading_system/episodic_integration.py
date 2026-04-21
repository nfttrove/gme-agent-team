"""Integration hooks for episodic memory logging.

Call after tasks complete to log predictions, trades, and signals to episodic memory.
Parsed outputs are validated through Pydantic models; malformed outputs return None
rather than corrupting the episodic log.
"""
import json
import logging
import re
import sys
import os

from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".agent"))
from episodic_logger import (
    log_prediction,
    log_trade,
    log_signal,
    log_synthesis,
)

from models.agent_outputs import (
    FuturistPrediction,
    TraderDecision,
    SynthesisBrief,
)

log = logging.getLogger(__name__)


def extract_prediction_from_output(agent_output: str, horizon: str = "1h") -> FuturistPrediction | None:
    """Parse Futurist agent output to extract a validated prediction."""
    try:
        json_match = re.search(r"\{[^}]*prediction[^}]*\}", agent_output, re.IGNORECASE)
        if not json_match:
            return None
        data = json.loads(json_match.group())
        return FuturistPrediction(
            predicted_price=float(data.get(horizon, {}).get("price", 0)),
            confidence=float(data.get("overall_confidence", 0)),
            horizon=horizon,
            bias=data.get("bias", "HOLD"),
            reasoning=data.get("self_reflection", ""),
        )
    except (json.JSONDecodeError, ValueError, KeyError, ValidationError) as e:
        log.debug(f"extract_prediction_from_output: {type(e).__name__}: {e}")
        return None


def extract_trade_from_output(agent_output: str) -> TraderDecision | None:
    """Parse Manager/Trader output to extract a validated trade decision."""
    try:
        json_match = re.search(r"\{[^}]*decision[^}]*\}", agent_output, re.IGNORECASE)
        if not json_match:
            return None
        data = json.loads(json_match.group())
        entry = float(data.get("entry_price", 0))
        if entry <= 0:
            return None
        qty_usd = float(data.get("quantity_usd", 0))
        return TraderDecision(
            action=data.get("action", "HOLD"),
            entry_price=entry,
            quantity=qty_usd / entry if entry else 0,
            stop_loss=float(data.get("stop_loss", 0)),
            take_profit=float(data.get("take_profit", 0)),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", ""),
        )
    except (json.JSONDecodeError, ValueError, KeyError, ValidationError) as e:
        log.debug(f"extract_trade_from_output: {type(e).__name__}: {e}")
        return None


def extract_synthesis_from_output(agent_output: str) -> SynthesisBrief | None:
    """Parse Synthesis agent output to extract a validated consensus brief.

    Expected format:
      PRICE: $XX | DATA: clean | NEWS: <label> <score> | ... | CONSENSUS: BULLISH 65%
    """
    try:
        fields: dict = {}

        price_match = re.search(r"PRICE:\s*\$?([\d.]+)", agent_output)
        if price_match:
            fields["price"] = float(price_match.group(1))

        data_match = re.search(r"DATA:\s*(\w+)", agent_output)
        if data_match:
            fields["data_quality"] = data_match.group(1)

        news_match = re.search(r"NEWS:\s*(\w+)\s+([\d.-]+)", agent_output)
        if news_match:
            fields["news_sentiment"] = float(news_match.group(2))

        pattern_match = re.search(r"PATTERN:\s*(\w+)", agent_output)
        if pattern_match:
            fields["pattern_type"] = pattern_match.group(1)

        trend_match = re.search(r"TREND:\s*(\w+)\s+([\d.]+)", agent_output)
        if trend_match:
            fields["trend_direction"] = trend_match.group(1)
            fields["trend_strength"] = float(trend_match.group(2))

        pred_match = re.search(r"PREDICTION:\s*(\w+)\s+([\d.]+)", agent_output)
        if pred_match:
            fields["prediction_bias"] = pred_match.group(1)
            fields["prediction_confidence"] = float(pred_match.group(2))

        struct_match = re.search(r"STRUCTURAL:\s*(\w+)", agent_output)
        if struct_match:
            fields["structural_status"] = struct_match.group(1)

        consensus_match = re.search(r"CONSENSUS:\s*(\w+)\s+([\d]+)%", agent_output)
        if consensus_match:
            fields["consensus"] = consensus_match.group(1)
            fields["consensus_pct"] = int(consensus_match.group(2)) / 100

        if not fields:
            return None
        return SynthesisBrief(**fields)

    except (ValueError, AttributeError, ValidationError) as e:
        log.debug(f"extract_synthesis_from_output: {type(e).__name__}: {e}")
        return None


def log_futurist_prediction(agent_output: str, horizon: str = "1h") -> str | None:
    """Log Futurist's prediction to episodic memory."""
    pred = extract_prediction_from_output(agent_output, horizon)
    if pred is None:
        return None

    episode_id = log_prediction(
        agent_name="Futurist",
        predicted_price=pred.predicted_price,
        confidence=pred.confidence,
        horizon=pred.horizon,
        bias=pred.bias,
        reasoning=pred.reasoning,
    )
    print(
        f"✓ Logged prediction: {pred.bias} @ {pred.predicted_price:.2f} "
        f"({pred.confidence:.0%} conf)"
    )
    return episode_id


def log_manager_trade(agent_output: str) -> str | None:
    """Log Manager/Trader's approved trade to episodic memory."""
    trade = extract_trade_from_output(agent_output)
    if trade is None:
        return None

    episode_id = log_trade(
        action=trade.action.value if hasattr(trade.action, "value") else str(trade.action),
        entry_price=trade.entry_price,
        quantity=trade.quantity,
        stop_loss=trade.stop_loss,
        take_profit=trade.take_profit,
        confidence=trade.confidence,
        reasoning=trade.reasoning,
        status="pending",
    )
    print(
        f"✓ Logged trade: {trade.action} {trade.quantity:.2f} @ {trade.entry_price:.2f}"
    )
    return episode_id


def log_synthesis_brief(agent_output: str) -> str | None:
    """Log Synthesis agent's consensus brief to episodic memory."""
    synth = extract_synthesis_from_output(agent_output)
    if synth is None:
        return None

    episode_id = log_synthesis(
        price=synth.price,
        data_quality=synth.data_quality,
        news_sentiment=synth.news_sentiment,
        pattern_type=synth.pattern_type,
        trend_direction=synth.trend_direction,
        trend_strength=synth.trend_strength,
        prediction_bias=synth.prediction_bias,
        prediction_confidence=synth.prediction_confidence,
        structural_status=synth.structural_status,
        consensus=synth.consensus,
        consensus_pct=synth.consensus_pct,
    )
    print(f"✓ Logged synthesis: {synth.consensus} {synth.consensus_pct:.0%}")
    return episode_id
