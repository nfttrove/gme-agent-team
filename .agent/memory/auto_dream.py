#!/usr/bin/env python3
"""
auto_dream.py — nightly staging cycle. Clusters episodic outcomes into candidate lessons.

Run via cron: 0 3 * * * python3 /path/to/.agent/memory/auto_dream.py

Purely mechanical (no reasoning):
  1. Load episodic trade logs
  2. Cluster by outcome + strategy + conditions
  3. Stage high-confidence clusters as candidates
  4. Human reviews with graduate.py / reject.py

No git commits, no reasoning, no network — safe to run unattended.
"""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

AGENT_ROOT = Path(__file__).parent.parent
EPISODIC_DIR = AGENT_ROOT / "memory" / "episodic"
STAGED_DIR = AGENT_ROOT / "memory" / "semantic" / "staged"

def load_episodic(days: int = 30) -> list:
    """Load recent trade logs."""
    trades_file = EPISODIC_DIR / "trades.jsonl"
    if not trades_file.exists():
        return []

    cutoff = datetime.utcnow() - timedelta(days=days)
    trades = []

    with open(trades_file) as f:
        for line in f:
            try:
                trade = json.loads(line)
                timestamp_str = trade.get("timestamp", "")
                if timestamp_str:
                    ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        trades.append(trade)
            except:
                pass

    return trades

def cluster_by_pattern(trades: list) -> dict:
    """Group trades by (action, outcome, condition)."""
    clusters = defaultdict(list)

    for trade in trades:
        action = trade.get("action", "unknown")
        outcome = trade.get("outcome", "unknown")
        condition = trade.get("tags", [])
        condition_str = "|".join(sorted(condition))

        key = f"{action}_{outcome}_{condition_str}"
        clusters[key].append(trade)

    return clusters

def is_high_confidence(cluster: list, min_examples: int = 3) -> bool:
    """Check if cluster has statistical weight."""
    if len(cluster) < min_examples:
        return False

    # All same outcome? (100% win rate)
    outcomes = [t.get("outcome") for t in cluster]
    if len(set(outcomes)) == 1:
        return True

    # Majority outcome >= 70%?
    outcome_counts = defaultdict(int)
    for o in outcomes:
        outcome_counts[o] += 1
    max_count = max(outcome_counts.values()) if outcome_counts else 0
    return max_count / len(cluster) >= 0.7

def stage_candidates(clusters: dict):
    """Write high-confidence clusters as staged candidates."""
    STAGED_DIR.mkdir(parents=True, exist_ok=True)

    for cluster_key, trades in clusters.items():
        if not is_high_confidence(trades):
            continue

        action, outcome, conditions = cluster_key.rsplit("_", 2)
        condition_tags = conditions.split("|") if conditions else []

        # Craft a claim
        if outcome == "profitable":
            claim = f"{action.upper()} strategy works for {', '.join(condition_tags)}"
        else:
            claim = f"Avoid {action.upper()} when {', '.join(condition_tags)}"

        # Craft why
        num_trades = len(trades)
        outcome_rate = sum(1 for t in trades if t.get("outcome") == outcome) / num_trades * 100
        avg_pnl = sum(t.get("pnl", 0) for t in trades) / num_trades

        why = f"{num_trades} trades, {outcome_rate:.0f}% {outcome}, avg PnL: ${avg_pnl:.0f}"

        candidate = {
            "pattern_id": f"{hash(cluster_key) & 0xfff:012x}",
            "claim": claim,
            "why": why,
            "status": "staged",
            "staged_at": datetime.utcnow().isoformat() + "Z",
            "examples": num_trades,
            "trades": [t.get("timestamp") for t in trades],
            "tags": condition_tags
        }

        candidate_file = STAGED_DIR / f"{candidate['pattern_id']}.json"
        with open(candidate_file, "w") as f:
            json.dump(candidate, f, indent=2)

        print(f"[auto_dream] Staged: {candidate['claim']}")

if __name__ == "__main__":
    trades = load_episodic(days=30)
    if not trades:
        print("[auto_dream] No trades found. Exiting.")
        exit(0)

    clusters = cluster_by_pattern(trades)
    stage_candidates(clusters)
    print(f"[auto_dream] Complete. {len(clusters)} clusters analyzed.")
