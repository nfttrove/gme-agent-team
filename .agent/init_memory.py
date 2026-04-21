#!/usr/bin/env python3
"""Initialize the episodic memory system.

Creates folders, seed lessons, and checks permissions.
"""
import os
import json
from datetime import datetime

MEMORY_ROOT = os.path.join(os.path.dirname(__file__), "memory")


def init_memory():
    """Create memory folder structure."""
    folders = [
        os.path.join(MEMORY_ROOT, "episodic"),
        os.path.join(MEMORY_ROOT, "semantic"),
        os.path.join(MEMORY_ROOT, "candidates"),
        os.path.join(MEMORY_ROOT, "working"),
    ]

    for folder in folders:
        os.makedirs(folder, exist_ok=True)
        print(f"✓ {folder}")

    # Initialize lessons.jsonl if it doesn't exist
    lessons_path = os.path.join(MEMORY_ROOT, "semantic", "lessons.jsonl")
    if not os.path.exists(lessons_path):
        with open(lessons_path, "w") as f:
            pass  # Create empty file
        print(f"✓ {lessons_path}")

    # Initialize episodes.jsonl if it doesn't exist
    episodes_path = os.path.join(MEMORY_ROOT, "episodic", "episodes.jsonl")
    if not os.path.exists(episodes_path):
        with open(episodes_path, "w") as f:
            pass  # Create empty file
        print(f"✓ {episodes_path}")

    # Initialize candidates.jsonl if it doesn't exist
    candidates_path = os.path.join(MEMORY_ROOT, "candidates", "candidates.jsonl")
    if not os.path.exists(candidates_path):
        with open(candidates_path, "w") as f:
            pass  # Create empty file
        print(f"✓ {candidates_path}")

    # Add seed lessons (PE playbook patterns)
    add_seed_lessons(lessons_path)

    print("\n✓ Memory system initialized!")
    print(f"\nNext steps:")
    print(f"  1. Run agents: python3 gme_trading_system/run_single_agent.py futurist")
    print(f"  2. Each night auto-discovery runs (requires cron setup)")
    print(f"  3. Review patterns: python3 .agent/list_candidates.py")
    print(f"  4. Graduate validated patterns: python3 .agent/graduate.py <id> --rationale '...'")


def add_seed_lessons(lessons_path: str) -> None:
    """Add seed lessons about GME and PE playbook patterns."""
    seed_lessons = [
        {
            "timestamp": datetime.utcnow().isoformat(),
            "pattern_id": "pe_playbook_stage5_critical",
            "type": "structural_signal",
            "conditions": {
                "signals": ["restructuring_advisor_hired", "cro_appointed", "debt_maturity_cliff"],
                "min_signals": 2,
            },
            "outcome": "Stage 5 Endgame — EXIT ALL EQUITY",
            "evidence": 1000,  # Historical evidence from PE playbook analysis
            "confidence": 0.99,
            "description": (
                "When ≥2 Stage 5 signals detected (restructuring advisor, CRO, debt cliff) → "
                "company is in endgame. Exit all equity positions. Zero recovery expected."
            ),
            "graduated_at": datetime.utcnow().isoformat(),
            "graduated_by": "seed",
            "rationale": "PE playbook historical pattern — well-documented failure mode",
        },
        {
            "timestamp": datetime.utcnow().isoformat(),
            "pattern_id": "gme_immunity_thesis",
            "type": "structural_signal",
            "conditions": {
                "debt": "zero",
                "cash": ">$1B",
                "board": "purged",
                "ceo": "cohen_aligned",
            },
            "outcome": "GME is structurally immune to PE playbook",
            "evidence": 1000,
            "confidence": 0.99,
            "description": (
                "GME meets all immunity conditions: zero debt, $9B+ cash, "
                "purged board, Cohen-aligned leadership. Immune to PE playbook destruction."
            ),
            "graduated_at": datetime.utcnow().isoformat(),
            "graduated_by": "seed",
            "rationale": "Core investment thesis — validated by fundamentals",
        },
        {
            "timestamp": datetime.utcnow().isoformat(),
            "pattern_id": "high_confidence_prediction_accuracy",
            "type": "prediction_accuracy",
            "conditions": {
                "confidence": ">0.70",
                "horizon": "1h",
            },
            "outcome": "High-confidence predictions tend to be accurate",
            "evidence": 100,
            "confidence": 0.75,
            "description": (
                "When Futurist predicts with >70% confidence → ~75% accuracy rate. "
                "Use high-confidence predictions to narrow stop losses."
            ),
            "graduated_at": datetime.utcnow().isoformat(),
            "graduated_by": "seed",
            "rationale": "Historical GME data — reasonable baseline",
        },
    ]

    # Append seed lessons if they don't already exist
    existing_ids = set()
    if os.path.exists(lessons_path):
        with open(lessons_path, "r") as f:
            for line in f:
                lesson = json.loads(line)
                existing_ids.add(lesson.get("pattern_id"))

    with open(lessons_path, "a") as f:
        for lesson in seed_lessons:
            if lesson["pattern_id"] not in existing_ids:
                f.write(json.dumps(lesson) + "\n")


if __name__ == "__main__":
    init_memory()
