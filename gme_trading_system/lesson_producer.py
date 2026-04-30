"""
lesson_producer.py — nightly job that mines SQLite outcome data for new
graduated lessons.

Why this exists: the consumer side of the learning loop is wired
(orchestrator injects lessons into Futurist's task description) but for
weeks only 3 hand-seeded lessons existed. The legacy producer pipelines
in .agent/memory/auto_dream.py and root cluster_patterns.py both depend
on .agent/memory/episodic/episodes.jsonl which is empty — no agent ever
populated it. The real outcome data lives in SQLite (signal_scores,
performance_scores, predictions, trade_decisions). This module reads
SQLite directly, scores patterns, and emits new graduated/staged
lessons in the same canonical schema learning.py already supports.

Generators are pluggable: register new ones in `GENERATORS` and each
runs once per produce_lessons() call. v1 ships with one — per-agent
directional bias from signal_scores.

Idempotency: pattern_ids are stable, so re-runs UPDATE the existing
record rather than appending duplicates. Both lessons.jsonl and
candidates.jsonl get fully rewritten each call.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
LESSONS_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", ".agent", "memory", "semantic",
    "lessons.jsonl",
))
CANDIDATES_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", ".agent", "memory", "candidates",
    "candidates.jsonl",
))

# Lookback for pattern mining. Long enough for a sample, short enough to
# track regime change (mirrors confidence_calibration's window).
LOOKBACK_DAYS = 30

# A pattern needs at least this many resolved signals to even be considered.
# Below this, noise dominates.
MIN_EVIDENCE = 10
# How far hit rate must deviate from coin-flip to count as an edge.
MIN_EDGE = 0.10

# Auto-graduate when both bars are cleared. Otherwise the candidate is
# staged in candidates.jsonl for human review via /candidates + /graduate.
AUTO_GRAD_N = 20
AUTO_GRAD_EDGE = 0.15


# ─── Generator: per-agent directional bias ───────────────────────────────────


def per_agent_directional_bias_candidates(conn: sqlite3.Connection,
                                          lookback_days: int = LOOKBACK_DAYS,
                                          ) -> list[dict]:
    """For each (agent, signal_type) with enough evidence and a real edge
    away from coin-flip, emit a candidate lesson.

    The candidate's `description` includes the raw hit rate so Futurist
    sees "fade your own price_prediction" (or "trust Pattern's continuation
    signal") in tomorrow's prompt.
    """
    rows = conn.execute(
        """
        SELECT agent_name, signal_type,
               COUNT(*) AS n,
               SUM(directional_hit) AS hits
        FROM signal_scores
        WHERE validated_at > datetime('now', ?)
          AND directional_hit IS NOT NULL
        GROUP BY agent_name, signal_type
        """,
        (f"-{lookback_days} days",),
    ).fetchall()

    out: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for agent, signal_type, n, hits in rows:
        if n is None or n < MIN_EVIDENCE:
            continue
        hit_rate = hits / n if n else 0.0
        # Round to dodge float-precision noise at the threshold boundary
        # (e.g. 0.6 - 0.5 = 0.09999... is "really" 0.10 and should qualify).
        edge = round(abs(hit_rate - 0.5), 4)
        if edge < MIN_EDGE:
            continue

        # Edge interpretation: if hit_rate < 0.5 the agent is wrong more
        # often than right — fade. If > 0.5, trust. The outcome string is
        # what shows up at the top of Futurist's prompt.
        direction = "trust" if hit_rate > 0.5 else "fade"
        outcome = (
            f"{agent}'s {signal_type} directional hits "
            f"{hit_rate:.0%} (n={n}) — {direction} as {direction}-side signal"
        )
        description = (
            f"Over {lookback_days}d lookback, {agent}'s {signal_type} signals "
            f"had directional_hit {hit_rate:.2f} ({hits}/{n}). "
            + ("Counter-indicator edge — invert direction in synthesis."
               if hit_rate < 0.5
               else "Positive edge — weight this signal in synthesis.")
        )
        candidate = {
            "pattern_id": f"directional_bias_{agent}_{signal_type}",
            "type": "directional_bias",
            "conditions": {"agent": agent, "signal_type": signal_type},
            "outcome": outcome,
            "evidence": int(n),
            # Confidence is how far from coin-flip we are, mapped to [0.5, 1].
            "confidence": round(0.5 + edge, 4),
            "description": description,
            "_metrics": {"hit_rate": round(hit_rate, 4),
                         "n": int(n), "edge": round(edge, 4)},
            "_timestamp": now_iso,
        }
        out.append(candidate)
    return out


# Pluggable list — v2 generators (multi-agent agreement, time-of-day,
# volatility regime) just append here.
GENERATORS: list[Callable[[sqlite3.Connection], list[dict]]] = [
    per_agent_directional_bias_candidates,
]


# ─── File I/O helpers ────────────────────────────────────────────────────────


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _upsert_by_pattern_id(rows: list[dict], new_row: dict) -> list[dict]:
    """Replace any existing row with the same pattern_id; otherwise append.
    Mirrors learner._upsert_score's UPSERT semantics for jsonl."""
    pid = new_row.get("pattern_id")
    if not pid:
        return rows + [new_row]
    return [r for r in rows if r.get("pattern_id") != pid] + [new_row]


# ─── Classification ──────────────────────────────────────────────────────────


def _qualifies_auto_graduate(candidate: dict) -> bool:
    m = candidate.get("_metrics", {})
    return m.get("n", 0) >= AUTO_GRAD_N and m.get("edge", 0.0) >= AUTO_GRAD_EDGE


def _to_graduated_lesson(candidate: dict, *, graduated_by: str) -> dict:
    """Strip the producer-internal `_metrics` / `_timestamp` and stamp the
    seeded canonical schema fields the consumer (learning.py) expects."""
    now_iso = candidate.pop("_timestamp", datetime.now(timezone.utc).isoformat())
    metrics = candidate.pop("_metrics", {})
    rationale = (
        f"n={metrics.get('n', '?')} ≥ {AUTO_GRAD_N} and "
        f"|hit_rate − 0.5| = {metrics.get('edge', 0):.2f} ≥ {AUTO_GRAD_EDGE}"
        if graduated_by == "auto_v1"
        else f"manual graduation via {graduated_by}"
    )
    return {
        **candidate,
        "graduated_at": now_iso,
        "graduated_by": graduated_by,
        "rationale": rationale,
    }


def _to_staged_candidate(candidate: dict) -> dict:
    """Stage for human review — keep the metrics so /candidates can show
    why it didn't auto-graduate."""
    now_iso = candidate.pop("_timestamp", datetime.now(timezone.utc).isoformat())
    return {
        **candidate,
        "status": "staged",
        "staged_at": now_iso,
    }


# ─── Entry point ─────────────────────────────────────────────────────────────


def produce_lessons(db_path: str = DB_PATH,
                    lessons_path: str = LESSONS_PATH,
                    candidates_path: str = CANDIDATES_PATH) -> dict:
    """Run all generators, classify each candidate as graduated vs staged,
    upsert into the appropriate jsonl file. Returns a summary dict for
    logging/Telegram.
    """
    conn = sqlite3.connect(db_path)
    try:
        all_candidates: list[dict] = []
        for gen in GENERATORS:
            try:
                all_candidates.extend(gen(conn))
            except Exception as e:
                log.warning(f"[lesson_producer] generator {gen.__name__} failed: {e}")
    finally:
        conn.close()

    lessons = _load_jsonl(lessons_path)
    candidates = _load_jsonl(candidates_path)

    auto_grad_count = 0
    staged_count = 0
    for cand in all_candidates:
        if _qualifies_auto_graduate(cand):
            lesson = _to_graduated_lesson(cand, graduated_by="auto_v1")
            lessons = _upsert_by_pattern_id(lessons, lesson)
            # If it was previously staged, drop the staged version.
            candidates = [c for c in candidates
                          if c.get("pattern_id") != lesson["pattern_id"]]
            auto_grad_count += 1
        else:
            staged = _to_staged_candidate(cand)
            candidates = _upsert_by_pattern_id(candidates, staged)
            staged_count += 1

    _write_jsonl(lessons_path, lessons)
    _write_jsonl(candidates_path, candidates)

    summary = {
        "candidates_generated": len(all_candidates),
        "auto_graduated": auto_grad_count,
        "staged": staged_count,
        "total_lessons": len(lessons),
        "total_staged": len([c for c in candidates if c.get("status") == "staged"]),
    }
    log.info(f"[lesson_producer] {summary}")
    return summary


# ─── Telegram helpers (used by /candidates, /graduate, /reject) ──────────────


def list_staged_candidates(candidates_path: Optional[str] = None) -> list[dict]:
    return [c for c in _load_jsonl(candidates_path or CANDIDATES_PATH)
            if c.get("status") == "staged"]


def find_candidate_by_short_id(short_id: str,
                               candidates_path: Optional[str] = None,
                               ) -> Optional[dict]:
    """Resolve a short pattern_id prefix (>=6 chars) to a staged candidate.
    Returns None if no unique match."""
    short = (short_id or "").strip().lower()
    if len(short) < 6:
        return None
    matches = [c for c in list_staged_candidates(candidates_path)
               if c.get("pattern_id", "").lower().startswith(short)]
    if len(matches) != 1:
        return None
    return matches[0]


def graduate_candidate(short_id: str, *,
                       graduated_by: str = "user_telegram",
                       lessons_path: Optional[str] = None,
                       candidates_path: Optional[str] = None,
                       ) -> Optional[dict]:
    """Promote one staged candidate to lessons.jsonl. Returns the graduated
    row, or None if not found / ambiguous."""
    lessons_path = lessons_path or LESSONS_PATH
    candidates_path = candidates_path or CANDIDATES_PATH
    cand = find_candidate_by_short_id(short_id, candidates_path)
    if cand is None:
        return None
    pid = cand["pattern_id"]
    # Strip staging-only fields before graduation.
    cand_clean = {k: v for k, v in cand.items()
                  if k not in ("status", "staged_at")}
    lessons = _load_jsonl(lessons_path)
    lesson = {
        **cand_clean,
        "graduated_at": datetime.now(timezone.utc).isoformat(),
        "graduated_by": graduated_by,
        "rationale": f"manual graduation via {graduated_by}",
    }
    lessons = _upsert_by_pattern_id(lessons, lesson)
    _write_jsonl(lessons_path, lessons)
    candidates = [c for c in _load_jsonl(candidates_path)
                  if c.get("pattern_id") != pid]
    _write_jsonl(candidates_path, candidates)
    return lesson


def reject_candidate(short_id: str,
                     candidates_path: Optional[str] = None,
                     ) -> Optional[str]:
    """Drop one staged candidate. Returns the pattern_id removed, or None."""
    candidates_path = candidates_path or CANDIDATES_PATH
    cand = find_candidate_by_short_id(short_id, candidates_path)
    if cand is None:
        return None
    pid = cand["pattern_id"]
    candidates = [c for c in _load_jsonl(candidates_path)
                  if c.get("pattern_id") != pid]
    _write_jsonl(candidates_path, candidates)
    return pid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(produce_lessons())
