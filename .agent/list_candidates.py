#!/usr/bin/env python3
"""List staged pattern candidates waiting for review.

Shows patterns discovered by cluster_patterns.py with evidence/confidence.
"""
import json
import os
import sys
from datetime import datetime

CANDIDATES_DIR = os.path.join(os.path.dirname(__file__), "memory", "candidates")


def list_candidates() -> None:
    """Display all staged candidates sorted by confidence."""
    candidates_path = os.path.join(CANDIDATES_DIR, "candidates.jsonl")

    if not os.path.exists(candidates_path):
        print("No candidates found. Run: python3 .agent/cluster_patterns.py")
        return

    candidates = []
    with open(candidates_path, "r") as f:
        for line in f:
            cand = json.loads(line)
            if cand.get("status") == "staged":
                candidates.append(cand)

    if not candidates:
        print("✓ No pending candidates. All staged patterns have been reviewed.")
        return

    # Sort by confidence (highest first)
    candidates.sort(key=lambda c: c.get("confidence", 0), reverse=True)

    print(f"\n📊 {len(candidates)} Pattern Candidates (highest confidence first)\n")
    print("=" * 100)

    for i, cand in enumerate(candidates, 1):
        pattern_id = cand.get("pattern_id", "unknown")
        cand_type = cand.get("type", "unknown")
        confidence = cand.get("confidence", 0)
        evidence = cand.get("evidence", 0)
        desc = cand.get("description", "")

        print(f"\n{i}. [{pattern_id}]")
        print(f"   Type: {cand_type} | Confidence: {confidence:.0%} | Evidence: {evidence} samples")
        print(f"   {desc}")
        print(f"\n   To accept:  python3 .agent/graduate.py {pattern_id} --rationale 'validation holds, applies to GME'")
        print(f"   To reject:  python3 .agent/reject.py {pattern_id} --reason 'too specific' or 'low signal'")
        print("-" * 100)


if __name__ == "__main__":
    list_candidates()
