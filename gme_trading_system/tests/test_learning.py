"""
Tests for learning.py — the bridge that surfaces graduated lessons into
agent task descriptions. Each test writes its own lessons.jsonl in a
tmp_path so we don't depend on production .agent state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import learning  # noqa: E402


def _write_lessons(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ─── load_lessons ────────────────────────────────────────────────────────────


def test_missing_file_returns_empty(tmp_path):
    """No lessons file yet → load returns []. Recall returns ""."""
    missing = tmp_path / "nope.jsonl"
    assert learning.load_lessons(str(missing)) == []
    assert learning.recall_relevant_lessons("anything", path=str(missing)) == ""


def test_filters_to_graduated_only(tmp_path):
    """Both schemas qualify: status=='graduated' OR a graduated_at timestamp."""
    p = tmp_path / "lessons.jsonl"
    _write_lessons(p, [
        {"outcome": "A", "description": "x", "status": "graduated"},
        {"outcome": "B", "description": "y", "graduated_at": "2026-01-01"},
        {"outcome": "C", "description": "z"},                   # neither — drop
        {"outcome": "D", "description": "w", "status": "candidate"},  # drop
    ])
    rows = learning.load_lessons(str(p))
    outcomes = {r["outcome"] for r in rows}
    assert outcomes == {"A", "B"}


def test_load_skips_malformed_lines(tmp_path):
    """A bad JSON line shouldn't poison the whole file."""
    p = tmp_path / "lessons.jsonl"
    p.write_text(
        '{"outcome": "good", "description": "ok", "graduated_at": "2026-01-01"}\n'
        'this is not json\n'
        '\n'
        '{"outcome": "good2", "description": "ok2", "graduated_at": "2026-01-02"}\n'
    )
    rows = learning.load_lessons(str(p))
    assert len(rows) == 2


# ─── recall_relevant_lessons ────────────────────────────────────────────────


def test_recall_returns_relevant_only(tmp_path):
    """Lessons whose words don't overlap the intent are filtered out."""
    p = tmp_path / "lessons.jsonl"
    _write_lessons(p, [
        {"outcome": "PE playbook endgame",
         "description": "When restructuring advisor and CRO appointed, exit equity.",
         "graduated_at": "2026-01-01"},
        {"outcome": "Sushi rolling techniques",
         "description": "How to roll temaki properly using nori.",
         "graduated_at": "2026-01-01"},
    ])
    out = learning.recall_relevant_lessons(
        "GME PE playbook restructuring exit", path=str(p))
    assert "PE playbook endgame" in out
    assert "Sushi" not in out
    assert "TEAM LESSONS LEARNED" in out


def test_recall_empty_when_no_overlap(tmp_path):
    """Below MIN_OVERLAP, no lessons returned (avoid noise injection)."""
    p = tmp_path / "lessons.jsonl"
    _write_lessons(p, [
        {"outcome": "Sushi rolling",
         "description": "How to roll temaki using nori sheets.",
         "graduated_at": "2026-01-01"},
    ])
    out = learning.recall_relevant_lessons(
        "GME options IV calendar spread", path=str(p))
    assert out == ""


def test_recall_caps_at_top_n(tmp_path):
    """top_n bullets max — don't flood the agent prompt."""
    p = tmp_path / "lessons.jsonl"
    _write_lessons(p, [
        {"outcome": f"GME lesson {i}",
         "description": f"GME trading strategy detail {i}",
         "graduated_at": "2026-01-01"}
        for i in range(10)
    ])
    out = learning.recall_relevant_lessons(
        "GME trading strategy", top_n=3, path=str(p))
    # Exactly 3 bullets (one '•' per lesson)
    assert out.count("•") == 3


def test_recall_format_includes_outcome_and_description(tmp_path):
    """Bullet must show both the headline outcome and the supporting desc."""
    p = tmp_path / "lessons.jsonl"
    _write_lessons(p, [
        {"outcome": "GME structurally immune to PE playbook",
         "description": "Zero debt, $9B+ cash, purged board — immune.",
         "graduated_at": "2026-01-01"},
    ])
    out = learning.recall_relevant_lessons("GME PE playbook", path=str(p))
    assert "GME structurally immune" in out
    assert "Zero debt" in out
    # Outcome and description joined with em-dash
    assert "—" in out


# ─── factory wiring ──────────────────────────────────────────────────────────


def test_make_futurist_task_embeds_lessons(tmp_path):
    """Lessons string passed to make_futurist_task lands in the description."""
    from tasks import make_futurist_task
    from agents import futurist_agent
    lessons = "TEAM LESSONS LEARNED:\n• something specific"
    task = make_futurist_task(futurist_agent, "$25.10", "no logs",
                              "no synthesis", [], lessons_str=lessons)
    assert "TEAM LESSONS LEARNED" in task.description
    assert "something specific" in task.description


def test_make_futurist_task_omits_block_when_empty():
    """No lessons → no extra "TEAM LESSONS LEARNED" preamble pollutes prompt."""
    from tasks import make_futurist_task
    from agents import futurist_agent
    task = make_futurist_task(futurist_agent, "$25.10", "no logs",
                              "no synthesis", [], lessons_str="")
    assert "TEAM LESSONS LEARNED" not in task.description
