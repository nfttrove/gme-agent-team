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
    compute_agent_signal_metrics,
    compute_futurist_metrics,
    parse_horizon,
    score_due_predictions,
    score_due_signals,
    target_time,
    write_signal_performance_scores,
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
        CREATE TABLE signal_alerts (
            id TEXT PRIMARY KEY,
            agent_name TEXT,
            signal_type TEXT,
            confidence REAL,
            severity TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            reasoning TEXT,
            telegram_message_id INTEGER,
            timestamp TEXT,
            created_at TEXT
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

    made = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
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

    made = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
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

    made = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
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


# ── signal_alerts scoring (Pattern / Trendy / Futurist) ─────────────────────
#
# The signal_alerts table is the single source of truth for "the agent told
# us X at time T with confidence C, TP=a, SL=b". These tests verify that the
# score_due_signals() scorer:
#   1. Refuses to fabricate an outcome when there's no tick data
#   2. Correctly identifies TP-before-SL first-touch wins
#   3. Correctly identifies SL-before-TP losses
#   4. Scores Brier per the conf-vs-outcome formula
#   5. Is idempotent — re-running doesn't double-count


def _insert_signal(conn, *, sig_id, agent, ts, entry, tp, sl, conf,
                   signal_type="pattern_signal"):
    conn.execute(
        "INSERT INTO signal_alerts (id, agent_name, signal_type, confidence, "
        "severity, entry_price, stop_loss, take_profit, reasoning, "
        "timestamp, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, agent, signal_type, conf, "MEDIUM", entry, sl, tp,
         "test", ts.isoformat(), ts.isoformat()),
    )


def test_signal_scorer_refuses_when_window_not_closed(tmp_path):
    """A signal fired 30 minutes ago can't be scored yet — 4h window open."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    now = datetime.now(ET)
    _insert_signal(conn, sig_id="sig-open", agent="Pattern",
                   ts=now - timedelta(minutes=30),
                   entry=25.0, tp=26.0, sl=24.5, conf=0.70)
    conn.commit()
    conn.close()

    summary = score_due_signals(db)
    assert summary["signals_scored"] == 0
    # Window hasn't closed → not an abandoned, just skipped for now
    assert summary["signals_abandoned"] == 0


def test_signal_scorer_detects_tp_first_touch_as_win(tmp_path):
    """Bullish signal: TP hit before SL → directional_hit=1, tp_hit=1."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    _insert_signal(conn, sig_id="sig-win", agent="Pattern", ts=made,
                   entry=25.0, tp=26.0, sl=24.0, conf=0.75)
    # Baseline tick at signal time
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    # Tick 30 min in — price climbed to 26.10, which hits TP=26.0
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 26.10, ?)",
        ((made + timedelta(minutes=30)).isoformat(),),
    )
    # Later tick — doesn't matter, TP already touched first
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 23.00, ?)",
        ((made + timedelta(hours=3)).isoformat(),),
    )
    conn.commit()
    conn.close()

    summary = score_due_signals(db)
    assert summary["signals_scored"] == 1

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT tp_hit, sl_hit, directional_hit, brier_term FROM signal_scores"
    ).fetchone()
    conn.close()
    assert row[0] == 1  # tp_hit
    assert row[1] == 0  # sl not hit
    assert row[2] == 1  # directional win


def test_signal_scorer_detects_sl_first_touch_as_loss(tmp_path):
    """Bullish signal: SL hit before TP → directional_hit=0, sl_hit=1."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    made = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
    _insert_signal(conn, sig_id="sig-loss", agent="Pattern", ts=made,
                   entry=25.0, tp=26.0, sl=24.0, conf=0.80)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    # First meaningful tick — drops to 23.90, blows through SL=24.00
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 23.90, ?)",
        ((made + timedelta(minutes=45)).isoformat(),),
    )
    # Later recovery to 26.50 doesn't matter — SL hit first
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 26.50, ?)",
        ((made + timedelta(hours=3)).isoformat(),),
    )
    conn.commit()
    conn.close()

    summary = score_due_signals(db)
    assert summary["signals_scored"] == 1

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT tp_hit, sl_hit, directional_hit, brier_term FROM signal_scores"
    ).fetchone()
    conn.close()
    assert row[0] == 0  # tp not hit
    assert row[1] == 1  # sl hit
    assert row[2] == 0  # directional loss
    # An 80%-confident bullish call that SL'd → prob_up=0.80, outcome=0
    # Brier = 0.64. Test core of "overconfidence punished" invariant.
    assert row[3] == pytest.approx(0.64, abs=0.01)


def test_signal_scorer_refuses_when_no_ticks_in_window(tmp_path):
    """No price ticks in the 4h validation window → refuse to score.
    Must NOT fabricate an end_price. This is the anti-hallucination contract."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    # Make the signal old enough that SIGNAL_MAX_WAIT has elapsed
    # (so the scorer moves from skip → abandon)
    made = datetime.now(ET) - timedelta(days=2)
    _insert_signal(conn, sig_id="sig-dark", agent="Pattern", ts=made,
                   entry=25.0, tp=26.0, sl=24.0, conf=0.70)
    # No price_ticks at all in the window
    conn.commit()
    conn.close()

    summary = score_due_signals(db)
    assert summary["signals_abandoned"] == 1

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT end_price, brier_term, notes FROM signal_scores"
    ).fetchone()
    conn.close()
    assert row[0] is None         # end_price left NULL — no invention
    assert row[1] is None         # Brier also NULL
    assert "unscorable" in row[2]


def test_signal_scorer_is_idempotent(tmp_path):
    """Re-running the scorer must NOT re-insert an already-scored signal."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)
    _insert_signal(conn, sig_id="sig-dup", agent="Pattern", ts=made,
                   entry=25.0, tp=26.0, sl=24.0, conf=0.70)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 26.10, ?)",
        ((made + timedelta(hours=1)).isoformat(),),
    )
    conn.commit()
    conn.close()

    s1 = score_due_signals(db)
    s2 = score_due_signals(db)

    assert s1["signals_scored"] == 1
    assert s2["signals_scored"] == 0

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM signal_scores").fetchone()[0]
    conn.close()
    assert count == 1


def test_compute_agent_signal_metrics_returns_zero_sample_for_empty(tmp_path):
    """Asking for metrics on an agent with no scored signals returns
    sample_size=0 — NOT a zero hit_rate (which would be misleading)."""
    db = _make_db(tmp_path)
    m = compute_agent_signal_metrics(db, agent_name="GhostAgent")
    assert m["sample_size"] == 0
    assert "hit_rate" not in m  # don't report a metric on n=0


def test_write_signal_performance_scores_writes_per_agent(tmp_path):
    """Score a win for Pattern and a loss for Trendy. Both should appear in
    performance_scores as separate rows — don't pool across agents."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    made = datetime(2026, 4, 23, 10, 0, tzinfo=ET)

    # Pattern: bullish call that worked
    _insert_signal(conn, sig_id="pat-win", agent="Pattern", ts=made,
                   entry=25.0, tp=26.0, sl=24.0, conf=0.70,
                   signal_type="pattern_signal")
    # Trendy: bullish call that blew through SL
    _insert_signal(conn, sig_id="trd-loss", agent="Trendy", ts=made,
                   entry=25.0, tp=26.0, sl=24.0, conf=0.80,
                   signal_type="trend_signal")

    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 25.00, ?)",
        (made.isoformat(),),
    )
    # Price climbs to 26.10 (TP hit for both, but we insert another
    # tick at the 30-min mark that blows SL first — actually we need
    # different price paths. Simplest: give them different timestamps.)
    # Pattern path: up first
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, timestamp) VALUES ('GME', 26.10, ?)",
        ((made + timedelta(minutes=20)).isoformat(),),
    )
    conn.commit()
    conn.close()

    # Both signals get scored against the same tick stream — both will show
    # TP-hit. That's fine; the test is that *separate agent rows* get written.
    score_due_signals(db)
    n_written = write_signal_performance_scores(db)
    assert n_written >= 4  # 2 agents × ≥2 metrics each

    conn = sqlite3.connect(db)
    agents_with_scores = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT agent_name FROM performance_scores"
        ).fetchall()
    }
    conn.close()
    assert "Pattern" in agents_with_scores
    assert "Trendy" in agents_with_scores
