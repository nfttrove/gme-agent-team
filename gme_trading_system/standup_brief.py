"""Verdict-first standup composition: categorize agents into LISTEN / MUTED,
persist daily gate decisions, and compute day-over-day status diffs.

Pure functions over the accuracy + gate-evaluator dicts, plus thin sqlite
helpers for the agent_gate_history table. The orchestrator wires it all
together; the formatter renders.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date


# Thresholds for the LISTEN bucket. Above coin-flip with margin, plus enough
# samples to not be noise. Trendy at 79% on n=14 still qualifies (high-accuracy
# small-sample) but gets flagged with a "small sample" caveat downstream.
LISTEN_HIT_MIN = 0.55
LISTEN_N_MIN = 10
LISTEN_SMALL_SAMPLE_N = 20  # below this, LISTEN row gets the "small sample" caveat


@dataclass(frozen=True)
class AgentVerdict:
    agent_name: str
    bucket: str  # "LISTEN" | "MUTED"
    sample_size: int
    hits_correct: int
    hit_rate: float | None  # 0..1
    tp_rate: float | None
    reason: str  # short tag explaining the bucket choice
    gate_decision: str  # "EMIT" | "SHADOW" | "SUPPRESS"
    small_sample: bool = False


def categorize_agent(
    agent_name: str,
    acc: dict,
    gate_decision: str,
) -> AgentVerdict:
    """Decide which bucket an agent goes in and why.

    acc must have keys: n, hit_rate (None if unscored), tp_rate.
    gate_decision is one of EMIT, SHADOW, SUPPRESS.
    """
    n = int(acc.get("n") or 0)
    hit = acc.get("hit_rate")
    tp_rate = acc.get("tp_rate")
    hits_correct = int(round((hit or 0) * n))

    if gate_decision == "SUPPRESS":
        reason = "statistically broken"
    elif gate_decision == "SHADOW":
        reason = "below trust threshold"
    elif n < LISTEN_N_MIN:
        reason = "sample too small"
    elif hit is None or hit < LISTEN_HIT_MIN:
        reason = "below trust threshold"
    else:
        # "only agent passing the bar" is added by the formatter when it
        # knows the trusted-list size; we just say "passing the bar" here.
        small = n < LISTEN_SMALL_SAMPLE_N
        return AgentVerdict(
            agent_name=agent_name, bucket="LISTEN", sample_size=n,
            hits_correct=hits_correct, hit_rate=hit, tp_rate=tp_rate,
            reason="passing the bar", gate_decision=gate_decision, small_sample=small,
        )

    return AgentVerdict(
        agent_name=agent_name, bucket="MUTED", sample_size=n,
        hits_correct=hits_correct, hit_rate=hit, tp_rate=tp_rate,
        reason=reason, gate_decision=gate_decision, small_sample=False,
    )


def categorize_all(acc_dict: dict, gate_decisions: dict) -> tuple[list[AgentVerdict], list[AgentVerdict]]:
    """Returns (trusted, muted) lists sorted by hit_rate descending."""
    verdicts = [
        categorize_agent(name, acc_dict[name], gate_decisions.get(name, "EMIT"))
        for name in acc_dict
    ]
    trusted = sorted([v for v in verdicts if v.bucket == "LISTEN"], key=lambda v: -(v.hit_rate or 0))
    muted = sorted([v for v in verdicts if v.bucket == "MUTED"], key=lambda v: -(v.hit_rate or 0))
    return trusted, muted


def ensure_gate_history_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_gate_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                gate_decision TEXT NOT NULL,
                hit_rate REAL,
                sample_size INTEGER,
                UNIQUE(snapshot_date, agent_name)
            );
            CREATE INDEX IF NOT EXISTS idx_agent_gate_history_date
                ON agent_gate_history(snapshot_date);
        """)


def save_gate_snapshot(
    db_path: str,
    verdicts: list[AgentVerdict],
    snapshot_date: str | None = None,
) -> None:
    """INSERT OR REPLACE today's gate decisions. Idempotent — safe to call
    twice on the same day (the 11:00 + 16:00 runs both fire)."""
    snapshot_date = snapshot_date or date.today().isoformat()
    if not verdicts:
        return
    rows = [
        (snapshot_date, v.agent_name, v.gate_decision, v.hit_rate, v.sample_size)
        for v in verdicts
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO agent_gate_history
                 (snapshot_date, agent_name, gate_decision, hit_rate, sample_size)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(snapshot_date, agent_name) DO UPDATE SET
                 gate_decision = excluded.gate_decision,
                 hit_rate = excluded.hit_rate,
                 sample_size = excluded.sample_size""",
            rows,
        )


def load_previous_gate_snapshot(db_path: str, before_date: str) -> dict[str, str]:
    """Returns {agent_name: gate_decision} from the most recent snapshot
    strictly before `before_date`. Empty dict if no prior snapshot exists."""
    with sqlite3.connect(db_path) as conn:
        try:
            latest = conn.execute(
                "SELECT MAX(snapshot_date) FROM agent_gate_history WHERE snapshot_date < ?",
                (before_date,),
            ).fetchone()
        except sqlite3.OperationalError:
            return {}
        if not latest or not latest[0]:
            return {}
        rows = conn.execute(
            "SELECT agent_name, gate_decision FROM agent_gate_history WHERE snapshot_date = ?",
            (latest[0],),
        ).fetchall()
    return {name: decision for name, decision in rows}


def compute_status_diff(
    today_verdicts: list[AgentVerdict],
    previous_decisions: dict[str, str],
) -> str:
    """One-line plain-English diff of gate decisions vs the previous snapshot."""
    if not previous_decisions:
        return "First snapshot recorded — diff begins tomorrow"

    today_decisions = {v.agent_name: v.gate_decision for v in today_verdicts}

    promotions = []   # name: prev_decision -> today_decision
    demotions = []
    for name, today_dec in today_decisions.items():
        prev_dec = previous_decisions.get(name)
        if not prev_dec or prev_dec == today_dec:
            continue
        # Rank: EMIT (3) > SHADOW (2) > SUPPRESS (1). Promote = score up.
        rank = {"SUPPRESS": 1, "SHADOW": 2, "EMIT": 3}
        if rank.get(today_dec, 0) > rank.get(prev_dec, 0):
            promotions.append(f"{name} promoted {prev_dec}→{today_dec}")
        else:
            demotions.append(f"{name} dropped {prev_dec}→{today_dec}")

    if not promotions and not demotions:
        return "unchanged since previous run"
    changes = promotions + demotions
    if len(changes) <= 2:
        return "; ".join(changes)
    return f"{len(changes)} agent statuses changed (see /standup for detail)"
