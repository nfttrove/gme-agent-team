#!/usr/bin/env python3
"""
show.py — colorful dashboard of brain state.

Usage:
  python3 .agent/tools/show.py
  python3 .agent/tools/show.py --json
"""
import json
import sys
from pathlib import Path
from datetime import datetime

AGENT_ROOT = Path(__file__).parent.parent
SEMANTIC_DIR = AGENT_ROOT / "memory" / "semantic"
EPISODIC_DIR = AGENT_ROOT / "memory" / "episodic"
WORKING_DIR = AGENT_ROOT / "memory" / "working"

def count_lessons() -> int:
    """Count graduated lessons."""
    lessons_file = SEMANTIC_DIR / "lessons.jsonl"
    if not lessons_file.exists():
        return 0
    count = 0
    with open(lessons_file) as f:
        for line in f:
            try:
                lesson = json.loads(line)
                if lesson.get("status") == "graduated":
                    count += 1
            except:
                pass
    return count

def count_trades() -> int:
    """Count logged trades."""
    trades_file = EPISODIC_DIR / "trades.jsonl"
    if not trades_file.exists():
        return 0
    with open(trades_file) as f:
        return len([line for line in f if line.strip()])

def show():
    lessons = count_lessons()
    trades = count_trades()

    print("\n" + "="*50)
    print("  AGENT BRAIN STATE")
    print("="*50)
    print(f"  Graduated lessons:  {lessons}")
    print(f"  Trade logs:         {trades}")
    print(f"  Session:            {datetime.now().isoformat()}")
    print("="*50 + "\n")

if __name__ == "__main__":
    show()
