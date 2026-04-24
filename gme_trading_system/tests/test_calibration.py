"""
Tests for calibration.py — horizon-aware prediction scoring.

Critical invariant: the scorer MUST refuse to invent a number when there's
no tick in the match window. The old learner scored every prediction against
EOD close regardless of horizon; we never want to regress to that.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calibration import (  # noqa: E402
    compute_futurist_metrics,
    parse_horizon,
    score_due_predictions,
    target_time,
)

ET = ZoneInfo("America/New_York")


def _make_db(tmp_path) -> str:
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE price_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            close REAL,
            timestamp TEXT
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            horizon TEXT,
            predicted_price REAL,
            confidence REAL,
            reasoning TEXT,
            actual_price REAL,
            error_pct REAL
        );
        CREATE TABLE performance_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            sample_size INTEGER DEFAULT 0,
            notes TEXT,
            UNIQUE(date, agent_name, metric)
        );
    """)
    conn.commit()
    conn.close()
    return db


# ── horizon parsing ──────────────────────────────────────────────────────────


def test_parse_horizon_hours():
    assert parse_horizon("1h") == timedelta(hours=1)
    assert parse_horizon("4h") == timedelta(hours=4)


def test_parse_horizon_minutes():
    assert parse_horizon("30m") == timedelta(minutes=30)


def test_parse_horizon_rejects_garbage():
    assert parse_horizon("EOD") is None
    assert parse_horizon("") is None
    assert parse_horizon(None) is None


def test_target_time_respects_horizon():
    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    assert target_time(made, "1h") == made + timedelta(hours=1)
    assert target_time(made, "4h") == made + timedelta(hours=4)


def test_target_time_eod_is_same_day_4pm_et():
    made = datetime(2026, 4, 23, 10, 30, tzinfo=ET)
    result = target_time(made, "EOD")
    assert result.hour == 16 and result.minute == 0
    assert result.date() == made.date()


# ── scoring correctness ──────────────────────────────────────────────────────


def test_scorer_uses_price_at_horizon_not_latest(tmp_path):
    """The whole point of this module: a 1h prediction made at 10:00 must be
    scored against the ~11:00 price, NOT against whatever the latest tick is.
    """
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    # Prediction made at 10:00, 1h horizon → target 11:00
    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    conn.execute(
        "INSERT INTO predictions (timestamp, horizon, predicted_price, confidence) "
        "VALUES (?, '1h', 25.00, 0.80)",
        (made.isoformat(),),
    )
    # Tick at 11:00 (the one that should be used): $24.50
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 24.50, ?)",
        ((made + timedelta(hours=1)).isoformat(),),
    )
    # Tick at 15:00 (should be IGNORED — it's the EOD-style distraction): $20.00
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 20.00, ?)",
        ((made + timedelta(hours=5)).isoformat(),),
    )
    conn.commit()
    conn.close()

    summary = score_due_predictions(db)

    assert summary["scored"] == 1

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT actual_price, error_pct FROM predictions"
    ).fetchone()
    conn.close()

    # Must have used the 1h tick ($24.50), not the 5h tick ($20.00)
    assert row[0] == pytest.approx(24.50)
    # error_pct = (24.50 - 25.00) / 25.00 * 100 = -2.0
    assert row[1] == pytest.approx(-2.0)


def test_scorer_refuses_to_invent_when_no_tick_in_window(tmp_path):
    """If there's no tick within ±5 min of the target, we MUST NOT write
    a fake actual_price. Better to leave it NULL than fabricate."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    conn.execute(
        "INSERT INTO predictions (timestamp, horizon, predicted_price, confidence) "
        "VALUES (?, '1h', 25.00, 0.80)",
        (made.isoformat(),),
    )
    # Only tick is 2 hours off target — well outside the 5 min window
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 24.00, ?)",
        ((made + timedelta(hours=3)).isoformat(),),
    )
    conn.commit()
    conn.close()

    summary = score_due_predictions(db)

    assert summary["scored"] == 0

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT actual_price, error_pct FROM predictions"
    ).fetchone()
    conn.close()

    # actual_price must remain NULL — we refuse to invent
    assert row[0] is None
    assert row[1] is None


def test_scorer_skips_not_yet_due_predictions(tmp_path):
    """A 1h prediction made 10 minutes ago shouldn't be scored — its window
    hasn't elapsed."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    made = datetime.now(ET) - timedelta(minutes=10)
    conn.execute(
        "INSERT INTO predictions (timestamp, horizon, predicted_price, confidence) "
        "VALUES (?, '1h', 25.00, 0.80)",
        (made.isoformat(),),
    )
    # Even if there's a tick right now, the prediction's horizon isn't up.
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 24.00, ?)",
        (datetime.now(ET).isoformat(),),
    )
    conn.commit()
    conn.close()

    summary = score_due_predictions(db)
    assert summary["scored"] == 0

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT actual_price FROM predictions").fetchone()
    conn.close()
    assert row[0] is None


# ── brier / hit-rate correctness ─────────────────────────────────────────────


def test_brier_penalises_overconfident_wrong_calls(tmp_path):
    """An 80%-confident bullish call that went the other way must produce a
    high Brier term (ideally >= 0.64). That's the whole point of Brier vs
    'average confidence' — it exposes overconfidence."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    # Baseline price at prediction time: $25
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    # Bullish call: predicted $26 (> baseline), 80% confidence
    conn.execute(
        "INSERT INTO predictions "
        "(timestamp, horizon, predicted_price, confidence, actual_price, error_pct) "
        "VALUES (?, '1h', 26.00, 0.80, 24.00, -7.6923)",
        (made.isoformat(),),
    )
    # Tick for baseline lookup at +1h (price went DOWN to $24)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 24.00, ?)",
        ((made + timedelta(hours=1)).isoformat(),),
    )
    conn.commit()
    conn.close()

    metrics = compute_futurist_metrics(db)

    assert metrics["sample_size"] == 1
    # Call was bullish (pred 26 > base 25), outcome bearish (actual 24 < base 25)
    # → hit = 0
    assert metrics["overall"]["hit_rate"] == 0.0
    # prob_up = 0.80 (bullish call), outcome = 0 → Brier term = 0.64
    assert metrics["overall"]["brier"] == pytest.approx(0.64, abs=0.01)


def test_write_performance_scores_is_idempotent(tmp_path):
    """performance_scores has UNIQUE(date, agent, metric). Running the
    calibrator twice on the same day must UPDATE, not fail on constraint."""
    from calibration import write_performance_scores

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(timestamp, horizon, predicted_price, confidence, actual_price, error_pct) "
        "VALUES (?, '1h', 26.00, 0.80, 25.50, -1.92)",
        (made.isoformat(),),
    )
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.50, ?)",
        ((made + timedelta(hours=1)).isoformat(),),
    )
    conn.commit()
    conn.close()

    # First write — should create rows
    n1 = write_performance_scores(db)
    assert n1 == 3  # mae, hit_rate, brier

    # Second write — must UPDATE existing rows, not raise IntegrityError
    n2 = write_performance_scores(db)
    assert n2 == 3

    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM performance_scores WHERE agent_name='Futurist'"
    ).fetchone()[0]
    conn.close()
    # Should still be exactly 3 rows (one per metric), not 6
    assert count == 3


def test_perfect_calibration_gives_zero_brier(tmp_path):
    """A 99%-confident call that was right should give Brier ≈ 0."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(timestamp, horizon, predicted_price, confidence, actual_price, error_pct) "
        "VALUES (?, '1h', 26.00, 0.99, 26.00, 0.0)",
        (made.isoformat(),),
    )
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 26.00, ?)",
        ((made + timedelta(hours=1)).isoformat(),),
    )
    conn.commit()
    conn.close()

    metrics = compute_futurist_metrics(db)
    assert metrics["overall"]["hit_rate"] == 1.0
    # prob_up = 0.99, outcome = 1 → Brier term = 0.0001
    assert metrics["overall"]["brier"] < 0.01
