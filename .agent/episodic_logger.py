"""Episodic memory logger for GME trading system.

Records predictions, trades, signals, and outcomes so patterns can be discovered.
Agents call this after completing their analysis.
"""
import json
import os
from datetime import datetime
from typing import Any

EPISODIC_DIR = os.path.join(os.path.dirname(__file__), "memory", "episodic")


def log_prediction(
    agent_name: str,
    predicted_price: float,
    confidence: float,
    horizon: str,
    bias: str,
    reasoning: str,
    constraints: dict = None,
) -> str:
    """Log a price prediction. Returns episode ID."""
    episode = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": "prediction",
        "agent": agent_name,
        "predicted_price": predicted_price,
        "confidence": confidence,
        "horizon": horizon,  # "1h", "4h", "24h"
        "bias": bias,  # "BUY", "SELL", "HOLD"
        "reasoning": reasoning,
        "constraints": constraints or {},
        "actual_price": None,  # filled later
        "outcome": None,  # "correct", "close", "wrong"
        "error_pct": None,
    }
    return _append_episode(episode)


def log_trade(
    action: str,
    entry_price: float,
    quantity: float,
    stop_loss: float,
    take_profit: float,
    confidence: float,
    reasoning: str,
    status: str = "pending",
) -> str:
    """Log a trade execution. Returns episode ID."""
    episode = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": "trade",
        "action": action,
        "entry_price": entry_price,
        "quantity": quantity,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "confidence": confidence,
        "reasoning": reasoning,
        "status": status,  # "pending", "filled", "rejected"
        "fill_price": None,
        "exit_price": None,
        "pnl": None,
        "pnl_pct": None,
    }
    return _append_episode(episode)


def log_signal(
    agent_name: str,
    signal_type: str,
    signal_name: str,
    strength: float,
    description: str,
    is_bullish: bool,
) -> str:
    """Log a detected pattern/signal. Returns episode ID."""
    episode = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": "signal",
        "agent": agent_name,
        "signal_type": signal_type,  # "pattern", "sentiment", "structure", etc
        "signal_name": signal_name,  # "symmetrical_triangle", "immunity_green", etc
        "strength": strength,  # 0-1
        "is_bullish": is_bullish,
        "description": description,
        "outcome": None,  # filled later: "correct", "incorrect"
    }
    return _append_episode(episode)


def log_synthesis(
    price: float,
    data_quality: str,
    news_sentiment: float,
    pattern_type: str,
    trend_direction: str,
    trend_strength: float,
    prediction_bias: str,
    prediction_confidence: float,
    structural_status: str,
    consensus: str,
    consensus_pct: float,
) -> str:
    """Log the team's consensus brief. Returns episode ID."""
    episode = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": "synthesis",
        "price": price,
        "data_quality": data_quality,  # "clean", "degraded"
        "news_sentiment": news_sentiment,  # -1 to 1
        "pattern": {
            "type": pattern_type,
            "trend_direction": trend_direction,
            "trend_strength": trend_strength,
        },
        "prediction": {
            "bias": prediction_bias,
            "confidence": prediction_confidence,
        },
        "structural": structural_status,  # "GREEN", "YELLOW", "RED"
        "consensus": consensus,  # "BULLISH", "BEARISH", "NEUTRAL"
        "consensus_pct": consensus_pct,
    }
    return _append_episode(episode)


def update_episode_outcome(
    episode_id: str,
    outcome: str,
    actual_value: Any = None,
    error_pct: float = None,
) -> bool:
    """Update an episode with actual outcome after the fact."""
    path = os.path.join(EPISODIC_DIR, f"{episode_id}.json")
    if not os.path.exists(path):
        return False
    with open(path, "r") as f:
        episode = json.load(f)
    episode["outcome"] = outcome
    if actual_value is not None:
        if episode["type"] == "prediction":
            episode["actual_price"] = actual_value
            episode["error_pct"] = error_pct
        elif episode["type"] == "signal":
            episode["correct"] = actual_value
    with open(path, "w") as f:
        json.dump(episode, f, indent=2)
    return True


def _append_episode(episode: dict) -> str:
    """Append episode to JSONL file and create individual JSON backup. Returns ID."""
    os.makedirs(EPISODIC_DIR, exist_ok=True)
    timestamp = episode["timestamp"].replace(":", "-").replace(".", "-")
    ep_type = episode["type"]
    episode_id = f"{ep_type}_{timestamp}"

    # Write individual JSON for easy access
    json_path = os.path.join(EPISODIC_DIR, f"{episode_id}.json")
    with open(json_path, "w") as f:
        json.dump(episode, f, indent=2)

    # Append to JSONL for analysis
    jsonl_path = os.path.join(EPISODIC_DIR, "episodes.jsonl")
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(episode) + "\n")

    return episode_id


def list_recent_episodes(ep_type: str = None, limit: int = 20) -> list[dict]:
    """List recent episodes, optionally filtered by type."""
    jsonl_path = os.path.join(EPISODIC_DIR, "episodes.jsonl")
    if not os.path.exists(jsonl_path):
        return []

    episodes = []
    with open(jsonl_path, "r") as f:
        for line in f:
            ep = json.loads(line)
            if ep_type is None or ep["type"] == ep_type:
                episodes.append(ep)

    return episodes[-limit:]
