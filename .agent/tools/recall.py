#!/usr/bin/env python3
"""
recall.py — surface lessons relevant to an intent.

Usage:
  python3 .agent/tools/recall.py "should I sell puts in high IV?"
  python3 .agent/tools/recall.py "GME gap down reversal"

Returns ranked hits by lexical overlap.
"""
import json
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).parent.parent
SEMANTIC_DIR = AGENT_ROOT / "memory" / "semantic"
LESSONS_FILE = SEMANTIC_DIR / "lessons.jsonl"
EPISODIC_DIR = AGENT_ROOT / "memory" / "episodic"

def jaccard_overlap(intent: str, lesson: str) -> float:
    """Simple lexical overlap: shared words / total words."""
    intent_words = set(intent.lower().split())
    lesson_words = set(lesson.lower().split())

    if not (intent_words | lesson_words):
        return 0.0

    intersection = len(intent_words & lesson_words)
    union = len(intent_words | lesson_words)
    return intersection / union if union > 0 else 0.0

def recall(intent: str, top_n: int = 5) -> list:
    """Surface top N lessons by relevance."""
    if not LESSONS_FILE.exists():
        return []

    lessons = []
    with open(LESSONS_FILE) as f:
        for line in f:
            try:
                lesson = json.loads(line)
                if lesson.get("status") == "graduated":
                    lessons.append(lesson)
            except json.JSONDecodeError:
                pass

    # Score by overlap with intent
    scored = []
    for lesson in lessons:
        claim = lesson.get("claim", "")
        why = lesson.get("why", "")
        text = f"{claim} {why}"
        score = jaccard_overlap(intent, text)
        if score > 0:
            scored.append((score, lesson))

    # Sort by score, descending
    scored.sort(reverse=True, key=lambda x: x[0])

    return [lesson for score, lesson in scored[:top_n]]

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: recall.py '<intent>'")
        sys.exit(1)

    intent = sys.argv[1]
    results = recall(intent)

    if not results:
        print(f"No lessons found for: {intent}")
        sys.exit(0)

    print(f"\n=== Recalled {len(results)} lesson(s) ===\n")
    for i, lesson in enumerate(results, 1):
        print(f"{i}. {lesson.get('claim')}")
        print(f"   Why: {lesson.get('why')}")
        print(f"   Status: {lesson.get('status')}")
        print()
