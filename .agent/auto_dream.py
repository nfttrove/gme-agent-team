#!/usr/bin/env python3
"""Nightly auto-discovery cycle — mechanical pattern clustering only.

No reasoning, no git commits, safe to run unattended via cron.
Runs: cluster_patterns.py → save candidates for morning review.

Usage:
  # Run once manually:
  python3 .agent/auto_dream.py

  # Run nightly at 3am:
  crontab -e
  0 3 * * * python3 /path/to/project/.agent/auto_dream.py >> /path/to/project/.agent/memory/dream.log 2>&1
"""
import os
import sys
import json
from datetime import datetime

# Add parent to path
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AGENT_DIR))

from cluster_patterns import discover_patterns, save_candidates


def run_dream_cycle():
    """Execute one nightly dream cycle."""
    print(f"[{datetime.utcnow().isoformat()}] Starting auto-dream cycle...")

    try:
        # Discover patterns from last 30 days of episodes
        candidates = discover_patterns(lookback_days=30)

        if not candidates:
            print(f"[{datetime.utcnow().isoformat()}] No new patterns discovered.")
            return

        # Save candidates for human review
        save_candidates(candidates)

        print(f"[{datetime.utcnow().isoformat()}] ✓ Dream cycle complete.")
        print(f"Staged {len(candidates)} candidates. Review with:")
        print("  python3 .agent/list_candidates.py")

    except Exception as e:
        print(f"[{datetime.utcnow().isoformat()}] Error during dream cycle: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_dream_cycle()
