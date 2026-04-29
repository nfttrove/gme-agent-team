"""
Per-agent confidence calibration applied at signal-emit time.

`calibration.py` already runs every 10 min and populates `signal_scores` with
directional_hit / tp_hit / brier_term per resolved signal. This module is the
*application* layer — it converts that historical accuracy into a multiplier
on stated confidence, applied at the moment a signal is emitted.

Why a multiplier (not a replacement): an agent's *relative* confidence still
carries information — when Futurist says 80% it usually means more conviction
than 60%. The calibration factor scales the level so the absolute number maps
to actual hit rate. If Futurist's avg stated conf is 70% but hit rate is 50%,
factor = 50/70 ≈ 0.71; an 80% signal becomes 57% effective.

Cold-start: until an agent has MIN_SAMPLE resolved signals, factor = 1.0 so
new agents aren't penalized for lack of history.

Clamped: factor ∈ [0.5, 1.5] so a single bad/lucky streak can't take an 80%
signal to 0% or 120%. The math should adjust direction, not invert the world.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

# Minimum resolved signals before we trust the calibration. Below this, factor
# stays at 1.0 — small-sample noise would be worse than the original confidence.
MIN_SAMPLE = 5

# Hard ceiling/floor on the multiplier. An 80% signal can't go below 40% or
# above 120% (clipped to 100% downstream). Keeps the system stable.
FACTOR_MIN = 0.5
FACTOR_MAX = 1.5

# Lookback window for "recent accuracy" — long enough to gather a sample,
# short enough to track regime change.
LOOKBACK_DAYS = 30


def get_agent_calibration(agent_name: str,
                          db_path: str = DB_PATH,
                          lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Return calibration stats for one agent, computed live from signal_scores.

    Returns a dict with:
      multiplier       — applied to stated confidence (1.0 = no change)
      hit_rate         — directional hit rate over the lookback window
      mean_stated_conf — mean of stated confidence on resolved signals
      sample_size      — number of resolved signals in window
      cold_start       — True if sample_size < MIN_SAMPLE
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Pull stated confidence from signal_alerts; outcome from signal_scores.
        rows = conn.execute(
            """
            SELECT sa.confidence AS stated, sc.directional_hit AS hit
            FROM signal_scores sc
            JOIN signal_alerts sa ON sa.id = sc.signal_id
            WHERE sc.agent_name = ?
              AND sc.validated_at > datetime('now', ?)
              AND sc.brier_term IS NOT NULL
              AND sa.confidence IS NOT NULL
            """,
            (agent_name, f"-{lookback_days} days"),
        ).fetchall()
    finally:
        conn.close()

    n = len(rows)
    cold = n < MIN_SAMPLE
    if n == 0:
        return {
            "multiplier": 1.0, "hit_rate": None, "mean_stated_conf": None,
            "sample_size": 0, "cold_start": True,
        }

    hits = sum(r["hit"] for r in rows) / n
    mean_conf = sum(r["stated"] for r in rows) / n

    if cold or mean_conf <= 0:
        factor = 1.0
    else:
        factor = max(FACTOR_MIN, min(FACTOR_MAX, hits / mean_conf))

    return {
        "multiplier": round(factor, 4),
        "hit_rate": round(hits, 4),
        "mean_stated_conf": round(mean_conf, 4),
        "sample_size": n,
        "cold_start": cold,
    }


def apply_to_confidence(stated_conf: float,
                        agent_name: str,
                        db_path: str = DB_PATH) -> tuple[float, dict]:
    """Return (effective_conf, calibration_metadata).

    effective_conf is clamped to [0.0, 1.0]. The original stated_conf is never
    mutated — callers can show both ("stated 80% / calibrated 57%") if they
    want transparency.
    """
    cal = get_agent_calibration(agent_name, db_path=db_path)
    eff = max(0.0, min(1.0, stated_conf * cal["multiplier"]))
    return eff, cal


def explain(agent_name: str, stated_conf: float,
            db_path: str = DB_PATH) -> str:
    """Human-readable one-liner for transparency in /standup, alerts, etc."""
    eff, cal = apply_to_confidence(stated_conf, agent_name, db_path)
    if cal["cold_start"]:
        return (f"{agent_name}: {stated_conf:.0%} (uncalibrated — "
                f"{cal['sample_size']} resolved signals, need {MIN_SAMPLE})")
    return (f"{agent_name}: stated {stated_conf:.0%} × "
            f"{cal['multiplier']:.2f} cal = {eff:.0%} effective "
            f"(hit_rate {cal['hit_rate']:.0%} on n={cal['sample_size']})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    conn = sqlite3.connect(DB_PATH)
    agents = [r[0] for r in conn.execute(
        "SELECT DISTINCT agent_name FROM signal_scores").fetchall()]
    conn.close()
    for a in agents:
        print(explain(a, 0.80))
