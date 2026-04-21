#!/usr/bin/env python3
"""
learn.py — teach a lesson immediately (stage + graduate in one step).

Usage:
  python3 .agent/tools/learn.py "Always check IV before selling puts" \\
    --why "IV crush risk — seen 3x in practice"

Idempotent: same input always produces same lesson ID.
"""
import json
import os
import sys
import hashlib
from datetime import datetime
from pathlib import Path

AGENT_ROOT = Path(__file__).parent.parent
SEMANTIC_DIR = AGENT_ROOT / "memory" / "semantic"
LESSONS_FILE = SEMANTIC_DIR / "lessons.jsonl"
LESSONS_MD = SEMANTIC_DIR / "LESSONS.md"

def canonicalize(text: str) -> str:
    """Normalize claim for idempotent ID."""
    return text.casefold().strip()

def derive_pattern_id(claim: str) -> str:
    """Hash of canonical claim."""
    canonical = canonicalize(claim)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]

def teach(claim: str, why: str, tag: str = "manual") -> str:
    """Graduate a lesson immediately."""
    pattern_id = derive_pattern_id(claim)
    timestamp = datetime.utcnow().isoformat() + "Z"

    lesson = {
        "pattern_id": pattern_id,
        "claim": claim,
        "why": why,
        "status": "graduated",
        "graduated_at": timestamp,
        "reviewed_by": "user",
        "rationale": why,
        "examples": 1,
        "tags": [tag]
    }

    SEMANTIC_DIR.mkdir(parents=True, exist_ok=True)

    # Append to lessons.jsonl
    with open(LESSONS_FILE, "a") as f:
        f.write(json.dumps(lesson) + "\n")

    print(f"✓ Lesson graduated: {pattern_id}")
    print(f"  {claim}")
    print(f"  Why: {why}")

    return pattern_id

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: learn.py '<claim>' --why '<rationale>'")
        sys.exit(1)

    claim = sys.argv[1]
    why = ""

    if "--why" in sys.argv:
        idx = sys.argv.index("--why")
        if idx + 1 < len(sys.argv):
            why = sys.argv[idx + 1]

    if not why:
        print("Error: --why is required")
        sys.exit(1)

    teach(claim, why)
