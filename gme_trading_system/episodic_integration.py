"""Integration hooks for episodic memory logging.

Call after tasks complete to log predictions, trades, and signals to episodic memory.
"""
import json
import re
from datetime import datetime
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from episodic_logger import (
    log_prediction,
    log_trade,
    log_signal,
    log_synthesis,
)


def extract_prediction_from_output(agent_output: str, horizon: str = "1h") -> dict:
    """Parse Futurist agent output to extract prediction."""
    try:
        # Look for JSON in output
        json_match = re.search(r"\{[^}]*prediction[^}]*\}", agent_output, re.IGNORECASE)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "predicted_price": float(data.get("1h", {}).get("price", 0)),
                "confidence": float(data.get("overall_confidence", 0)),
                "bias": data.get("bias", "HOLD"),
                "reasoning": data.get("self_reflection", ""),
            }
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return None


def extract_trade_from_output(agent_output: str) -> dict:
    """Parse Manager/Trader output to extract trade details."""
    try:
        json_match = re.search(r"\{[^}]*decision[^}]*\}", agent_output, re.IGNORECASE)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "action": data.get("action", "HOLD"),
                "entry_price": float(data.get("entry_price", 0)),
                "quantity": float(data.get("quantity_usd", 0)) / float(data.get("entry_price", 1)),
                "stop_loss": float(data.get("stop_loss", 0)),
                "take_profit": float(data.get("take_profit", 0)),
                "confidence": float(data.get("confidence", 0.5)),
                "reasoning": data.get("reasoning", ""),
            }
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return None


def extract_synthesis_from_output(agent_output: str) -> dict:
    """Parse Synthesis agent output to extract consensus."""
    try:
        # Parse pipe-separated format
        # Expected: "PRICE: $XX | DATA: clean | NEWS: X | ... | CONSENSUS: BULLISH 65%"
        result = {}

        # Extract price
        price_match = re.search(r"PRICE:\s*\$?([\d.]+)", agent_output)
        if price_match:
            result["price"] = float(price_match.group(1))

        # Extract data quality
        data_match = re.search(r"DATA:\s*(\w+)", agent_output)
        if data_match:
            result["data_quality"] = data_match.group(1)

        # Extract news sentiment
        news_match = re.search(r"NEWS:\s*(\w+)\s+([\d.-]+)", agent_output)
        if news_match:
            result["news_sentiment"] = float(news_match.group(2))

        # Extract pattern
        pattern_match = re.search(r"PATTERN:\s*(\w+)", agent_output)
        if pattern_match:
            result["pattern_type"] = pattern_match.group(1)

        # Extract trend
        trend_match = re.search(r"TREND:\s*(\w+)\s+([\d.]+)", agent_output)
        if trend_match:
            result["trend_direction"] = trend_match.group(1)
            result["trend_strength"] = float(trend_match.group(2))

        # Extract prediction
        pred_match = re.search(r"PREDICTION:\s*(\w+)\s+([\d.]+)", agent_output)
        if pred_match:
            result["prediction_bias"] = pred_match.group(1)
            result["prediction_confidence"] = float(pred_match.group(2))

        # Extract structural
        struct_match = re.search(r"STRUCTURAL:\s*(\w+)", agent_output)
        if struct_match:
            result["structural_status"] = struct_match.group(1)

        # Extract consensus
        consensus_match = re.search(r"CONSENSUS:\s*(\w+)\s+([\d]+)%", agent_output)
        if consensus_match:
            result["consensus"] = consensus_match.group(1)
            result["consensus_pct"] = int(consensus_match.group(2)) / 100

        return result if result else None

    except (ValueError, AttributeError):
        pass
    return None


def log_futurist_prediction(agent_output: str, horizon: str = "1h") -> str:
    """Log Futurist's prediction to episodic memory."""
    pred = extract_prediction_from_output(agent_output, horizon)
    if not pred:
        return None

    episode_id = log_prediction(
        agent_name="Futurist",
        predicted_price=pred["predicted_price"],
        confidence=pred["confidence"],
        horizon=horizon,
        bias=pred["bias"],
        reasoning=pred["reasoning"],
    )
    print(f"✓ Logged prediction: {pred['bias']} @ {pred['predicted_price']:.2f} ({pred['confidence']:.0%} conf)")
    return episode_id


def log_manager_trade(agent_output: str) -> str:
    """Log Manager/Trader's approved trade to episodic memory."""
    trade = extract_trade_from_output(agent_output)
    if not trade:
        return None

    episode_id = log_trade(
        action=trade["action"],
        entry_price=trade["entry_price"],
        quantity=trade["quantity"],
        stop_loss=trade["stop_loss"],
        take_profit=trade["take_profit"],
        confidence=trade["confidence"],
        reasoning=trade["reasoning"],
        status="pending",
    )
    print(
        f"✓ Logged trade: {trade['action']} {trade['quantity']:.2f} @ {trade['entry_price']:.2f}"
    )
    return episode_id


def log_synthesis_brief(agent_output: str) -> str:
    """Log Synthesis agent's consensus brief to episodic memory."""
    synth = extract_synthesis_from_output(agent_output)
    if not synth:
        return None

    episode_id = log_synthesis(
        price=synth.get("price", 0),
        data_quality=synth.get("data_quality", "unknown"),
        news_sentiment=synth.get("news_sentiment", 0),
        pattern_type=synth.get("pattern_type", "none"),
        trend_direction=synth.get("trend_direction", "sideways"),
        trend_strength=synth.get("trend_strength", 0),
        prediction_bias=synth.get("prediction_bias", "HOLD"),
        prediction_confidence=synth.get("prediction_confidence", 0.5),
        structural_status=synth.get("structural_status", "YELLOW"),
        consensus=synth.get("consensus", "NEUTRAL"),
        consensus_pct=synth.get("consensus_pct", 0.5),
    )
    print(f"✓ Logged synthesis: {synth.get('consensus')} {synth.get('consensus_pct'):.0%}")
    return episode_id
