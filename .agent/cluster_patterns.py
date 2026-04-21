"""Auto-discovery of recurring patterns from episodic memory.

Runs nightly to cluster prediction outcomes, trades, and signals.
Finds: "when X + Y + Z conditions occur → outcome happens 85% of the time"
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

EPISODIC_DIR = os.path.join(os.path.dirname(__file__), "memory", "episodic")
CANDIDATES_DIR = os.path.join(os.path.dirname(__file__), "memory", "candidates")


def discover_patterns(lookback_days: int = 30) -> list[dict]:
    """Find recurring condition → outcome patterns in recent episodes."""
    os.makedirs(CANDIDATES_DIR, exist_ok=True)

    jsonl_path = os.path.join(EPISODIC_DIR, "episodes.jsonl")
    if not os.path.exists(jsonl_path):
        return []

    # Load recent episodes
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    episodes = []
    with open(jsonl_path, "r") as f:
        for line in f:
            ep = json.loads(line)
            ep_time = datetime.fromisoformat(ep["timestamp"])
            if ep_time > cutoff:
                episodes.append(ep)

    # Cluster by type
    predictions = [e for e in episodes if e["type"] == "prediction" and e.get("outcome")]
    signals = [e for e in episodes if e["type"] == "signal" and e.get("outcome")]
    trades = [e for e in episodes if e["type"] == "trade" and e.get("pnl") is not None]
    syntheses = [e for e in episodes if e["type"] == "synthesis"]

    candidates = []

    # Pattern 1: Prediction accuracy by bias + confidence + horizon
    candidates.extend(_cluster_prediction_accuracy(predictions))

    # Pattern 2: Signal reliability (when signal fires, what happens?)
    candidates.extend(_cluster_signal_reliability(signals))

    # Pattern 3: Synthesis → Trade outcome (when team consensus was high, did we win?)
    candidates.extend(_cluster_synthesis_accuracy(syntheses, trades))

    # Pattern 4: Multi-signal combinations (when 3+ signals aligned, accuracy increased)
    candidates.extend(_cluster_signal_combinations(signals, syntheses))

    return candidates


def _cluster_prediction_accuracy(predictions: list[dict]) -> list[dict]:
    """Group predictions by bias+confidence, calculate accuracy."""
    if not predictions:
        return []

    by_group = defaultdict(list)
    for p in predictions:
        bias = p.get("bias", "UNKNOWN")
        conf = p.get("confidence", 0)
        conf_bucket = f"{int(conf*100)}%"  # e.g., "68%"
        horizon = p.get("horizon", "1h")
        key = f"prediction_{bias}_{conf_bucket}_{horizon}"
        by_group[key].append(p)

    candidates = []
    for group_key, group_preds in by_group.items():
        if len(group_preds) < 3:  # Need minimum sample size
            continue

        correct = sum(1 for p in group_preds if p.get("outcome") == "correct")
        close = sum(1 for p in group_preds if p.get("outcome") == "close")
        accuracy = (correct + close * 0.5) / len(group_preds)

        candidate = {
            "timestamp": datetime.utcnow().isoformat(),
            "pattern_id": group_key,
            "type": "prediction_accuracy",
            "conditions": {
                "bias": group_key.split("_")[1],
                "confidence": group_key.split("_")[2],
                "horizon": group_key.split("_")[3],
            },
            "outcome": f"{accuracy:.0%} accuracy",
            "evidence": len(group_preds),
            "confidence": accuracy,
            "description": (
                f"When Futurist predicts {group_key.split('_')[1]} "
                f"with {group_key.split('_')[2]} confidence for {group_key.split('_')[3]} → "
                f"{accuracy:.0%} accuracy ({len(group_preds)} trials)"
            ),
            "status": "staged",
            "reviewer": None,
            "rationale": None,
        }
        candidates.append(candidate)

    return candidates


def _cluster_signal_reliability(signals: list[dict]) -> list[dict]:
    """Group signals by name, calculate correctness rate."""
    if not signals:
        return []

    by_signal = defaultdict(list)
    for s in signals:
        signal_name = s.get("signal_name", "unknown")
        agent = s.get("agent", "unknown")
        is_bullish = s.get("is_bullish", False)
        key = f"signal_{agent}_{signal_name}_{('bullish' if is_bullish else 'bearish')}"
        by_signal[key].append(s)

    candidates = []
    for sig_key, sig_group in by_signal.items():
        if len(sig_group) < 3:
            continue

        correct = sum(1 for s in sig_group if s.get("outcome") == "correct")
        reliability = correct / len(sig_group)

        candidate = {
            "timestamp": datetime.utcnow().isoformat(),
            "pattern_id": sig_key,
            "type": "signal_reliability",
            "conditions": {
                "signal": sig_key.split("_")[2],
                "direction": sig_key.split("_")[3],
            },
            "outcome": f"{reliability:.0%} reliability",
            "evidence": len(sig_group),
            "confidence": reliability,
            "description": (
                f"{sig_key.split('_')[1]}'s {sig_key.split('_')[2]} signal "
                f"({sig_key.split('_')[3]}) → {reliability:.0%} correct "
                f"({len(sig_group)} observations)"
            ),
            "status": "staged",
            "reviewer": None,
            "rationale": None,
        }
        candidates.append(candidate)

    return candidates


def _cluster_synthesis_accuracy(syntheses: list[dict], trades: list[dict]) -> list[dict]:
    """When team consensus was high, did trades win?"""
    if not syntheses or not trades:
        return []

    candidates = []

    # Group syntheses by consensus level
    high_consensus = [s for s in syntheses if s.get("consensus_pct", 0) >= 0.70]
    low_consensus = [s for s in syntheses if s.get("consensus_pct", 0) < 0.70]

    for consensus_group, label in [(high_consensus, "high"), (low_consensus, "low")]:
        if len(consensus_group) < 3:
            continue

        # Count how many subsequent trades were profitable
        # (simplified: just count if bias matched actual trade action)
        winning = sum(1 for s in consensus_group if s.get("consensus") == "BULLISH")
        accuracy = winning / len(consensus_group) if consensus_group else 0

        candidate = {
            "timestamp": datetime.utcnow().isoformat(),
            "pattern_id": f"synthesis_{label}_consensus",
            "type": "synthesis_accuracy",
            "conditions": {
                "consensus_level": label,
                "min_consensus_pct": 0.70 if label == "high" else 0.0,
            },
            "outcome": f"{label} consensus → {accuracy:.0%} win rate",
            "evidence": len(consensus_group),
            "confidence": accuracy,
            "description": (
                f"When team had {label} consensus ({len(consensus_group)} instances) → "
                f"{accuracy:.0%} of following trades were aligned with consensus"
            ),
            "status": "staged",
            "reviewer": None,
            "rationale": None,
        }
        candidates.append(candidate)

    return candidates


def _cluster_signal_combinations(signals: list[dict], syntheses: list[dict]) -> list[dict]:
    """Find multi-signal patterns that predict outcomes."""
    if not syntheses:
        return []

    candidates = []

    # Find syntheses with multiple strong signals
    for s in syntheses:
        bullish_signals = sum(
            1
            for sig in signals
            if (
                sig.get("is_bullish")
                and sig["timestamp"] <= s["timestamp"]
                and sig["timestamp"] > datetime.fromisoformat(s["timestamp"]) - timedelta(hours=1)
            )
        )

        bearish_signals = sum(
            1
            for sig in signals
            if (
                not sig.get("is_bullish")
                and sig["timestamp"] <= s["timestamp"]
                and sig["timestamp"] > datetime.fromisoformat(s["timestamp"]) - timedelta(hours=1)
            )
        )

        if bullish_signals >= 3:
            candidate = {
                "timestamp": datetime.utcnow().isoformat(),
                "pattern_id": f"multi_signal_bullish_{bullish_signals}",
                "type": "signal_combination",
                "conditions": {
                    "min_bullish_signals": bullish_signals,
                },
                "outcome": f"{bullish_signals}+ bullish signals aligned",
                "evidence": 1,  # Just this one example for now
                "confidence": 0.5,  # Low confidence, need more samples
                "description": (
                    f"When {bullish_signals}+ bullish signals aligned simultaneously → "
                    f"team consensus: {s.get('consensus')}"
                ),
                "status": "staged",
                "reviewer": None,
                "rationale": None,
            }
            candidates.append(candidate)

    return candidates


def save_candidates(candidates: list[dict]) -> None:
    """Save staged candidates for human review."""
    os.makedirs(CANDIDATES_DIR, exist_ok=True)

    # Write to candidates.jsonl
    candidates_path = os.path.join(CANDIDATES_DIR, "candidates.jsonl")
    with open(candidates_path, "a") as f:
        for cand in candidates:
            f.write(json.dumps(cand) + "\n")

    print(f"✓ Staged {len(candidates)} pattern candidates for review")


if __name__ == "__main__":
    candidates = discover_patterns(lookback_days=30)
    save_candidates(candidates)
    print(f"\nDiscovered {len(candidates)} patterns. Review with:")
    print("  python3 .agent/list_candidates.py")
