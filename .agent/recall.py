#!/usr/bin/env python3
"""Surface lessons relevant to the current task.

Loads graduated patterns from semantic memory and surfaces them to agents.
Called before tasks to inject validated patterns into context.
"""
import json
import os
import sys
from typing import Any

SEMANTIC_DIR = os.path.join(os.path.dirname(__file__), "memory", "semantic")


def recall_lessons(task_type: str = None, query: str = None, limit: int = 5) -> list[dict]:
    """Get relevant lessons for the current task.

    Args:
        task_type: Filter by type (e.g., "prediction_accuracy", "signal_reliability")
        query: Keyword filter (e.g., "bullish", "confidence")
        limit: Max lessons to return

    Returns:
        List of graduated lessons sorted by relevance
    """
    lessons_path = os.path.join(SEMANTIC_DIR, "lessons.jsonl")

    if not os.path.exists(lessons_path):
        return []

    lessons = []
    with open(lessons_path, "r") as f:
        for line in f:
            lesson = json.loads(line)

            # Filter by type if specified
            if task_type and lesson.get("type") != task_type:
                continue

            # Filter by keyword if specified
            if query:
                if query.lower() not in lesson.get("description", "").lower():
                    continue

            lessons.append(lesson)

    # Sort by confidence (highest first)
    lessons.sort(key=lambda l: l.get("confidence", 0), reverse=True)
    return lessons[:limit]


def format_lesson(lesson: dict) -> str:
    """Format a lesson for display in agent context."""
    return (
        f"📌 VALIDATED PATTERN:\n"
        f"   {lesson['description']}\n"
        f"   Confidence: {lesson['confidence']:.0%} | Evidence: {lesson['evidence']} samples\n"
    )


def inject_lessons_into_prompt(agent_role: str) -> str:
    """Get relevant lessons for an agent's next task.

    Returns markdown that can be injected into task context.
    """
    # Map agent roles to task types they care about
    role_queries = {
        "Market Futurist": ("prediction_accuracy", "bias"),
        "Intelligence Synthesiser": ("synthesis_accuracy", "consensus"),
        "Chief Technology": ("signal_reliability", "structure"),
        "Data Validator": ("signal_reliability", "data"),
    }

    task_type, query = role_queries.get(agent_role, (None, None))
    lessons = recall_lessons(task_type=task_type, query=query, limit=3)

    if not lessons:
        return ""

    prompt = "\n## Validated Patterns from Prior Experience\n\n"
    for lesson in lessons:
        prompt += format_lesson(lesson)
    return prompt


def show_all_lessons() -> None:
    """Display all graduated lessons."""
    lessons = recall_lessons(limit=100)

    if not lessons:
        print("No graduated lessons yet. Run: python3 .agent/cluster_patterns.py")
        print("Then review candidates with: python3 .agent/list_candidates.py")
        return

    print(f"\n📚 {len(lessons)} Graduated Lessons\n")
    print("=" * 100)

    for lesson in lessons:
        print(f"• {lesson['description']}")
        print(f"  Confidence: {lesson['confidence']:.0%} | Evidence: {lesson['evidence']} samples")
        print(f"  Pattern ID: {lesson['pattern_id']}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--show-all":
        show_all_lessons()
    else:
        # Quick test: show lessons for Futurist
        print("Lessons for Market Futurist:\n")
        lessons = recall_lessons(task_type="prediction_accuracy", limit=5)
        if lessons:
            for lesson in lessons:
                print(format_lesson(lesson))
        else:
            print("No lessons yet.")
