"""
Tests for lesson_producer.py — the SQL-driven nightly producer that
emits new graduated lessons / staged candidates from signal_scores.

Each test seeds its own tmp SQLite (mirrors test_confidence_calibration's
approach) so we don't depend on prod data. Lessons + candidates files are
also tmp_path-scoped per test.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import lesson_producer as lp  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _make_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE signal_scores (
            signal_id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            signal_type TEXT,
            validated_at TEXT NOT NULL,
            directional_hit INTEGER,
            brier_term REAL
        )
        """
    )
    conn.commit()
    conn.close()


def _seed(db_path: Path, agent: str, signal_type: str,
          n: int, hits: int):
    """Insert n rows with `hits` directional_hit=1 and (n-hits) =0."""
    conn = sqlite3.connect(db_path)
    for i in range(n):
        h = 1 if i < hits else 0
        conn.execute(
            "INSERT INTO signal_scores (signal_id, agent_name, signal_type, "
            " validated_at, directional_hit, brier_term) "
            "VALUES (?, ?, ?, datetime('now'), ?, 0.1)",
            (f"{agent}-{signal_type}-{i:04d}", agent, signal_type, h),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "agent.db"
    _make_db(db)
    lessons = tmp_path / "lessons.jsonl"
    cands = tmp_path / "candidates" / "candidates.jsonl"
    return {"db": str(db), "lessons": str(lessons), "candidates": str(cands)}


def _produce(env):
    return lp.produce_lessons(env["db"], env["lessons"], env["candidates"])


def _read(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ─── Auto-graduate vs stage thresholds ───────────────────────────────────────


def test_auto_graduates_above_threshold(env):
    """n=25, hit_rate=0.20 → edge=0.30 → both bars cleared → auto-graduates."""
    _seed(Path(env["db"]), "Futurist", "price_prediction", n=25, hits=5)
    summary = _produce(env)
    assert summary["auto_graduated"] == 1
    assert summary["staged"] == 0
    lessons = _read(env["lessons"])
    assert len(lessons) == 1
    assert lessons[0]["graduated_by"] == "auto_v1"
    assert lessons[0]["pattern_id"] == "directional_bias_Futurist_price_prediction"
    # 0.20 hit rate < 0.5 → fade signal
    assert "fade" in lessons[0]["outcome"].lower()


def test_stages_below_auto_grad_n(env):
    """n=15 (below AUTO_GRAD_N=20) but edge=0.20 still meets MIN_EDGE.
    Should stage, NOT auto-graduate."""
    _seed(Path(env["db"]), "Pattern", "trend_continuation", n=15, hits=3)
    summary = _produce(env)
    assert summary["auto_graduated"] == 0
    assert summary["staged"] == 1
    assert _read(env["lessons"]) == []
    cands = _read(env["candidates"])
    assert len(cands) == 1
    assert cands[0]["status"] == "staged"


def test_stages_below_auto_grad_edge(env):
    """n=25 (clears AUTO_GRAD_N) but hit_rate=0.40 → edge=0.10 (< AUTO_GRAD_EDGE=0.15).
    Should stage, NOT auto-graduate."""
    _seed(Path(env["db"]), "Trendy", "daily_trend", n=25, hits=10)
    summary = _produce(env)
    assert summary["auto_graduated"] == 0
    assert summary["staged"] == 1


def test_skips_weak_edges(env):
    """n=50 but hit_rate=0.48 → edge=0.02 → below MIN_EDGE → no candidate."""
    _seed(Path(env["db"]), "Newsie", "sentiment_signal", n=50, hits=24)
    summary = _produce(env)
    assert summary["candidates_generated"] == 0
    assert _read(env["lessons"]) == []
    assert _read(env["candidates"]) == []


def test_skips_below_min_evidence(env):
    """n=8 (below MIN_EVIDENCE=10) is too small even with strong-looking edge."""
    _seed(Path(env["db"]), "GeoRisk", "macro_signal", n=8, hits=1)
    summary = _produce(env)
    assert summary["candidates_generated"] == 0


# ─── Idempotency ─────────────────────────────────────────────────────────────


def test_idempotent_same_input(env):
    """Two runs over identical data → identical files (no duplicates)."""
    _seed(Path(env["db"]), "Futurist", "price_prediction", n=25, hits=5)
    s1 = _produce(env)
    lessons1 = _read(env["lessons"])
    s2 = _produce(env)
    lessons2 = _read(env["lessons"])
    assert s1["auto_graduated"] == s2["auto_graduated"] == 1
    assert len(lessons1) == len(lessons2) == 1
    # Stable pattern_id → same row identity, just refreshed timestamps.
    assert lessons1[0]["pattern_id"] == lessons2[0]["pattern_id"]


def test_updates_existing_pattern(env):
    """More evidence accrues; same pattern_id → existing row replaced, not
    duplicated. Evidence count grows."""
    db = Path(env["db"])
    _seed(db, "Futurist", "price_prediction", n=25, hits=5)  # 20% hit, n=25
    _produce(env)
    first = _read(env["lessons"])[0]
    assert first["evidence"] == 25
    # Add 25 more misses → cumulative 50 rows, 5 hits → 10% (stronger fade edge)
    conn = sqlite3.connect(db)
    for i in range(25):
        conn.execute(
            "INSERT INTO signal_scores (signal_id, agent_name, signal_type, "
            " validated_at, directional_hit, brier_term) "
            "VALUES (?, 'Futurist', 'price_prediction', datetime('now'), 0, 0.1)",
            (f"Futurist-price_prediction-update-{1000 + i}",),
        )
    conn.commit()
    conn.close()
    _produce(env)
    lessons = _read(env["lessons"])
    assert len(lessons) == 1                 # not duplicated
    assert lessons[0]["evidence"] == 50      # updated with new sample size
    assert "fade" in lessons[0]["outcome"].lower()


# ─── Multi-agent ─────────────────────────────────────────────────────────────


def test_multi_agent_classification(env):
    """Three agents, three different patterns → each gets its own pattern_id
    and lands in the right file (lessons vs candidates)."""
    db = Path(env["db"])
    _seed(db, "Futurist", "price_prediction", n=30, hits=6)   # 20% → auto-grad
    _seed(db, "Pattern", "trend_continuation", n=12, hits=2)  # 17%, n<20 → stage
    _seed(db, "Trendy", "daily_trend", n=40, hits=20)         # 50% → skip
    summary = _produce(env)
    assert summary["auto_graduated"] == 1
    assert summary["staged"] == 1
    lessons = _read(env["lessons"])
    cands = _read(env["candidates"])
    assert {l["conditions"]["agent"] for l in lessons} == {"Futurist"}
    assert {c["conditions"]["agent"] for c in cands} == {"Pattern"}


# ─── Promote / reject helpers (Telegram path) ────────────────────────────────


def test_graduate_candidate_promotes_and_removes(env):
    """Manual /graduate flow: stage → graduate → no longer in candidates."""
    _seed(Path(env["db"]), "Pattern", "trend_continuation", n=15, hits=3)
    _produce(env)
    cands = _read(env["candidates"])
    assert len(cands) == 1
    pid = cands[0]["pattern_id"]
    # Use a 6-char prefix
    promoted = lp.graduate_candidate(pid[:10],
                                     lessons_path=env["lessons"],
                                     candidates_path=env["candidates"])
    assert promoted is not None
    assert promoted["graduated_by"] == "user_telegram"
    assert _read(env["lessons"])[0]["pattern_id"] == pid
    assert _read(env["candidates"]) == []


def test_reject_candidate_removes(env):
    _seed(Path(env["db"]), "Pattern", "trend_continuation", n=15, hits=3)
    _produce(env)
    cands = _read(env["candidates"])
    pid = cands[0]["pattern_id"]
    removed = lp.reject_candidate(pid[:10],
                                  candidates_path=env["candidates"])
    assert removed == pid
    assert _read(env["candidates"]) == []
    assert _read(env["lessons"]) == []   # was never graduated


def test_short_id_must_be_min_length(env):
    """Reject a too-short id outright — prevents accidental wrong matches."""
    _seed(Path(env["db"]), "Pattern", "trend_continuation", n=15, hits=3)
    _produce(env)
    assert lp.find_candidate_by_short_id("abc",
                                          candidates_path=env["candidates"]) is None


def test_lookback_excludes_old_signals(env):
    """Signals older than lookback don't pollute current calibration."""
    db = Path(env["db"])
    conn = sqlite3.connect(db)
    # Old + wrong: 60d ago, all wrong (would auto-grad to "fade" if counted)
    for i in range(30):
        conn.execute(
            "INSERT INTO signal_scores (signal_id, agent_name, signal_type, "
            " validated_at, directional_hit, brier_term) "
            "VALUES (?, 'Futurist', 'price_prediction', "
            " datetime('now', '-60 days'), 0, 0.1)",
            (f"old-{i}",),
        )
    conn.commit()
    conn.close()
    summary = _produce(env)
    # All are outside the 30d window → no candidates generated
    assert summary["candidates_generated"] == 0
