"""
learning.py — bridge from .agent/memory/semantic/lessons.jsonl into live agent
prompts.

Background: orchestrator.recall_lessons() has been calling .agent/tools/recall.py
for weeks. The output was logged and then discarded — never injected into any
agent's task description. Worse, recall.py filters lessons by `status ==
"graduated"`, but the seeded entries use `graduated_at` (no status field), so
recall.py returned nothing even when called.

This module:
  - reads lessons.jsonl directly (no subprocess)
  - accepts both `status == "graduated"` and `graduated_at` shapes
  - scores by simple jaccard overlap on description + outcome
  - returns a bulleted block ready to drop into a Task description

Why simple jaccard instead of embeddings: latency budget per cycle is tight,
the seeded set is small (3 lessons today), and lexical overlap is enough to
distinguish "GME PE playbook" from "options IV management". Upgrade to
embeddings only when the lesson count makes overlap noisy.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

LESSONS_FILE = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", ".agent", "memory", "semantic",
    "lessons.jsonl",
))

# Below this jaccard score, the lesson is more noise than signal — skip.
MIN_OVERLAP = 0.05


def _is_graduated(row: dict) -> bool:
    """Two schemas in the wild: explicit status, or graduated_at timestamp."""
    return row.get("status") == "graduated" or bool(row.get("graduated_at"))


_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Common stopwords inflate the union and mute overlap signal — drop them.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "in", "is", "it", "its", "of", "on", "or", "that", "the", "to",
    "was", "were", "with",
})


def _tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower())
            if t not in _STOPWORDS and len(t) > 1}


def _jaccard(intent: str, text: str) -> float:
    a = _tokenize(intent)
    b = _tokenize(text)
    if not (a | b):
        return 0.0
    return len(a & b) / len(a | b)


def load_lessons(path: str = LESSONS_FILE) -> list[dict]:
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_graduated(row):
                out.append(row)
    return out


def recall_relevant_lessons(intent: str, top_n: int = 3,
                            path: str = LESSONS_FILE) -> str:
    """Return a bulleted lessons block for `intent`, or "" if none qualify.

    Format is designed to drop into a Task description — agents see it
    verbatim at the top of their prompt.
    """
    lessons = load_lessons(path)
    if not lessons:
        return ""
    scored: list[tuple[float, dict]] = []
    for lesson in lessons:
        # Combine outcome + description for the overlap signal — outcome is
        # usually the headline ("GME is structurally immune to PE playbook"),
        # description carries the conditions/reasoning.
        text = f"{lesson.get('description', '')} {lesson.get('outcome', '')}"
        score = _jaccard(intent, text)
        if score >= MIN_OVERLAP:
            scored.append((score, lesson))
    if not scored:
        return ""
    scored.sort(reverse=True, key=lambda x: x[0])
    bullets = []
    for _, lesson in scored[:top_n]:
        outcome = lesson.get("outcome") or "(no outcome)"
        desc = (lesson.get("description") or "").strip()
        if desc:
            bullets.append(f"• {outcome} — {desc}")
        else:
            bullets.append(f"• {outcome}")
    return "TEAM LESSONS LEARNED (apply when relevant):\n" + "\n".join(bullets)
