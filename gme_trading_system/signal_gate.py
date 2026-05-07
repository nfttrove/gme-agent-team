"""Signal-layer accuracy gate.

Read rolling 30-day directional accuracy from signal_scores and decide whether
an agent's signal should be emitted (Telegram + signal_alerts), shadowed
(prediction logged but no alert), or fully suppressed.

Thresholds are deliberately wide so a few coin-flip days don't yank a working
agent. SHADOW vs SUPPRESS only differs in observability — both still write to
agent_logs so accuracy tracking continues regardless.

Why per-agent gating beats a global confidence floor: confidence numbers are
self-reported by the agent, but directional_hit is measured against actual
price moves. An agent that's 14% directionally accurate is broken in a way no
confidence threshold can rescue.
"""
import os
import sqlite3
from typing import Literal

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

Decision = Literal["EMIT", "SHADOW", "SUPPRESS"]

# Thresholds chosen so coin-flip noise doesn't trip the gate. Below 0.30 is
# statistically anomalous at n>=20 — directional accuracy that bad implies
# inverted logic or stale inputs, not unlucky variance.
HIT_RATE_EMIT_FLOOR = 0.50
HIT_RATE_SUPPRESS_FLOOR = 0.30
MIN_SAMPLE_SIZE = 20
LOOKBACK_DAYS = 30


def evaluate(agent_name: str, db_path: str = DB_PATH) -> dict:
    """Returns {decision, reason, hit_rate, sample_size}.

    decision is EMIT (allow Telegram + signal_alerts), SHADOW (skip alert,
    keep prediction in agent_logs), or SUPPRESS (skip alert, log gate
    decision).
    """
    conn = sqlite3.connect(db_path)
    try:
        # signal_scores is created lazily by calibration.py — fall back to
        # EMIT if it doesn't exist yet (first runs after fresh deploy).
        try:
            # Exclude flat windows (baseline == end_price). When a signal's 4h
            # window sits after the 16:00 ET close, ticks don't move and the
            # validator scores them as "wrong direction" by default, biasing
            # hit-rate down. Cleaner to drop them here than re-grade history.
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n, COALESCE(AVG(directional_hit), 0) AS hit_rate
                FROM signal_scores
                WHERE agent_name = ?
                  AND validated_at > datetime('now', '-{LOOKBACK_DAYS} days')
                  AND baseline_price != end_price
                """,
                (agent_name,),
            ).fetchone()
        except sqlite3.OperationalError:
            return {
                "decision": "EMIT",
                "reason": "signal_scores table missing (calibration not yet run)",
                "hit_rate": 0.0,
                "sample_size": 0,
            }
    finally:
        conn.close()

    n = int(row[0]) if row else 0
    hit_rate = float(row[1]) if row else 0.0

    if n < MIN_SAMPLE_SIZE:
        return {
            "decision": "EMIT",
            "reason": f"insufficient sample (n={n} < {MIN_SAMPLE_SIZE})",
            "hit_rate": round(hit_rate, 3),
            "sample_size": n,
        }

    if hit_rate >= HIT_RATE_EMIT_FLOOR:
        decision: Decision = "EMIT"
    elif hit_rate >= HIT_RATE_SUPPRESS_FLOOR:
        decision = "SHADOW"
    else:
        decision = "SUPPRESS"

    return {
        "decision": decision,
        "reason": f"{LOOKBACK_DAYS}d dir={hit_rate:.0%} (n={n})",
        "hit_rate": round(hit_rate, 3),
        "sample_size": n,
    }


def should_emit(agent_name: str, db_path: str = DB_PATH) -> tuple[bool, dict]:
    """Convenience wrapper: returns (allow_emit, gate_info)."""
    info = evaluate(agent_name, db_path)
    return info["decision"] == "EMIT", info
