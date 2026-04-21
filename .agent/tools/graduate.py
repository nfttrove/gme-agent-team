#!/usr/bin/env python3
"""
graduate.py — accept a staged candidate lesson.

Usage:
  python3 .agent/tools/graduate.py <pattern_id> --rationale "evidence holds"

Appends to lessons.jsonl and re-renders LESSONS.md.
Requires rationale to prevent rubber-stamping.
"""
import json
import sys
from pathlib import Path
from datetime import datetime

AGENT_ROOT = Path(__file__).parent.parent
SEMANTIC_DIR = AGENT_ROOT / "memory" / "semantic"
STAGED_DIR = SEMANTIC_DIR / "staged"
LESSONS_FILE = SEMANTIC_DIR / "lessons.jsonl"
LESSONS_MD = SEMANTIC_DIR / "LESSONS.md"

def graduate(pattern_id: str, rationale: str):
    """Move staged candidate to graduated lessons."""
    if not rationale:
        print("Error: --rationale is required (prevents rubber-stamping)")
        sys.exit(1)

    staged_file = STAGED_DIR / f"{pattern_id}.json"
    if not staged_file.exists():
        print(f"Error: no staged candidate found: {pattern_id}")
        sys.exit(1)

    with open(staged_file) as f:
        candidate = json.load(f)

    # Graduate the lesson
    lesson = {
        "pattern_id": candidate.get("pattern_id", pattern_id),
        "claim": candidate.get("claim", ""),
        "why": candidate.get("why", ""),
        "status": "graduated",
        "graduated_at": datetime.utcnow().isoformat() + "Z",
        "reviewed_by": "user",
        "rationale": rationale,
        "examples": candidate.get("examples", 1),
        "tags": candidate.get("tags", [])
    }

    LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Append to lessons.jsonl
    with open(LESSONS_FILE, "a") as f:
        f.write(json.dumps(lesson) + "\n")

    # Archive the staged candidate
    archived = STAGED_DIR / "archived" / f"{pattern_id}.json"
    archived.parent.mkdir(parents=True, exist_ok=True)
    staged_file.rename(archived)

    print(f"✓ Graduated: {pattern_id}")
    print(f"  {candidate.get('claim')}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: graduate.py <pattern_id> --rationale '<reason>'")
        sys.exit(1)

    pattern_id = sys.argv[1]
    rationale = ""

    if "--rationale" in sys.argv:
        idx = sys.argv.index("--rationale")
        if idx + 1 < len(sys.argv):
            rationale = sys.argv[idx + 1]

    graduate(pattern_id, rationale)
