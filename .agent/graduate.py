#!/usr/bin/env python3
"""Graduate a staged pattern candidate to semantic memory (lessons.jsonl).

Once graduated, the lesson will be loaded in future sessions when relevant.
"""
import json
import os
import sys
from datetime import datetime

CANDIDATES_DIR = os.path.join(os.path.dirname(__file__), "memory", "candidates")
SEMANTIC_DIR = os.path.join(os.path.dirname(__file__), "memory", "semantic")


def graduate_pattern(pattern_id: str, rationale: str) -> bool:
    """Move a candidate from staged to graduated lessons."""
    if not rationale:
        print("Error: --rationale is required. Example:")
        print("  python3 .agent/graduate.py pattern_xyz --rationale 'evidence holds, applies to GME'")
        return False

    candidates_path = os.path.join(CANDIDATES_DIR, "candidates.jsonl")
    os.makedirs(SEMANTIC_DIR, exist_ok=True)

    # Find the candidate
    candidate = None
    with open(candidates_path, "r") as f:
        for line in f:
            cand = json.loads(line)
            if cand.get("pattern_id") == pattern_id:
                candidate = cand
                break

    if not candidate:
        print(f"Error: Pattern '{pattern_id}' not found in candidates")
        return False

    # Graduate it
    lesson = {
        "timestamp": datetime.utcnow().isoformat(),
        "pattern_id": candidate.get("pattern_id"),
        "type": candidate.get("type"),
        "conditions": candidate.get("conditions"),
        "outcome": candidate.get("outcome"),
        "evidence": candidate.get("evidence"),
        "confidence": candidate.get("confidence"),
        "description": candidate.get("description"),
        "graduated_at": datetime.utcnow().isoformat(),
        "graduated_by": "human_review",
        "rationale": rationale,
    }

    # Append to lessons.jsonl
    lessons_path = os.path.join(SEMANTIC_DIR, "lessons.jsonl")
    with open(lessons_path, "a") as f:
        f.write(json.dumps(lesson) + "\n")

    # Mark candidate as graduated
    candidate["status"] = "graduated"
    candidate["graduated_at"] = datetime.utcnow().isoformat()

    # Rewrite candidates file
    with open(candidates_path, "w") as f:
        with open(candidates_path, "r+") as tmp:
            lines = tmp.readlines()
        with open(candidates_path, "w") as f:
            for line in lines:
                cand = json.loads(line)
                if cand.get("pattern_id") == pattern_id:
                    cand["status"] = "graduated"
                f.write(json.dumps(cand) + "\n")

    print(f"✓ Graduated {pattern_id}")
    print(f"  Confidence: {lesson['confidence']:.0%}")
    print(f"  Evidence: {lesson['evidence']} samples")
    print(f"  Rationale: {rationale}")
    print(f"\nLesson will load in future sessions when conditions match.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 .agent/graduate.py <pattern_id> --rationale '<reason>'")
        sys.exit(1)

    pattern_id = sys.argv[1]
    rationale = ""

    if "--rationale" in sys.argv:
        idx = sys.argv.index("--rationale")
        if idx + 1 < len(sys.argv):
            rationale = sys.argv[idx + 1]

    success = graduate_pattern(pattern_id, rationale)
    sys.exit(0 if success else 1)
