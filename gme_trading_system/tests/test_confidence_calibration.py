"""
Tests for confidence_calibration — the bridge that takes per-agent historical
hit_rate from signal_scores and adjusts stated confidence at emit time.

Each test seeds its own DB so we don't depend on production data, and
exercises the calibration through the same path notifier.notify_signal_alert
will use.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make gme_trading_system importable when tests are run from repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

import confidence_calibration as cc  # noqa: E402


def _make_db(path: Path):
    """Create the two tables the calibration reads from. Schemas mirror prod."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE signal_alerts (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            signal_type TEXT,
            confidence REAL,
            timestamp TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE signal_scores (
            signal_id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            signal_type TEXT,
            validated_at TEXT NOT NULL,
            baseline_price REAL,
            end_price REAL,
            tp_hit INTEGER NOT NULL DEFAULT 0,
            sl_hit INTEGER NOT NULL DEFAULT 0,
            directional_hit INTEGER NOT NULL DEFAULT 0,
            brier_term REAL,
            notes TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _seed(db_path: Path, agent: str, signals: list[tuple[float, int]]):
    """Insert paired (stated_conf, hit) rows. validated_at = now so they're
    inside the lookback window."""
    conn = sqlite3.connect(db_path)
    for i, (conf, hit) in enumerate(signals):
        sid = f"{agent}-{i:04d}"
        conn.execute(
            "INSERT INTO signal_alerts (id, agent_name, signal_type, "
            " confidence, timestamp) VALUES (?, ?, 'price_prediction', ?, "
            " datetime('now'))",
            (sid, agent, conf),
        )
        conn.execute(
            "INSERT INTO signal_scores (signal_id, agent_name, signal_type, "
            " validated_at, directional_hit, brier_term) "
            "VALUES (?, ?, 'price_prediction', datetime('now'), ?, 0.1)",
            (sid, agent, hit),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def empty_db(tmp_path):
    p = tmp_path / "calibration.db"
    _make_db(p)
    return p


# ─── cold-start behaviour ────────────────────────────────────────────────────


def test_unknown_agent_returns_neutral(empty_db):
    """No history → multiplier 1.0, cold_start True. Don't penalize new agents."""
    cal = cc.get_agent_calibration("Newcomer", db_path=str(empty_db))
    assert cal["multiplier"] == 1.0
    assert cal["sample_size"] == 0
    assert cal["cold_start"] is True


def test_below_min_sample_returns_neutral(empty_db):
    """Below MIN_SAMPLE → factor stays 1.0 even if hit rate is wildly off
    stated confidence. Small samples produce more noise than signal."""
    # Seed 4 signals (under MIN_SAMPLE=5), all wrong despite high stated conf
    _seed(empty_db, "Futurist", [(0.90, 0), (0.90, 0), (0.90, 0), (0.90, 0)])
    cal = cc.get_agent_calibration("Futurist", db_path=str(empty_db))
    assert cal["sample_size"] == 4
    assert cal["cold_start"] is True
    assert cal["multiplier"] == 1.0


# ─── meaningful adjustments above threshold ──────────────────────────────────


def test_overconfident_agent_dampened(empty_db):
    """Stated 80% but only hits 40% → multiplier ≈ 0.5 → 80% becomes 40%."""
    # 10 signals all stated 0.8, half hit
    signals = [(0.80, 1), (0.80, 1), (0.80, 1), (0.80, 1), (0.80, 0),
               (0.80, 0), (0.80, 0), (0.80, 0), (0.80, 0), (0.80, 0)]
    _seed(empty_db, "Futurist", signals)
    cal = cc.get_agent_calibration("Futurist", db_path=str(empty_db))
    assert cal["sample_size"] == 10
    assert cal["cold_start"] is False
    assert cal["hit_rate"] == 0.4
    assert cal["mean_stated_conf"] == 0.8
    # 0.4 / 0.8 = 0.5
    assert cal["multiplier"] == pytest.approx(0.5, abs=0.01)

    eff, _ = cc.apply_to_confidence(0.80, "Futurist", db_path=str(empty_db))
    assert eff == pytest.approx(0.40, abs=0.01)


def test_underconfident_agent_boosted(empty_db):
    """Stated 50% but hits 75% → multiplier 1.5 (clamp ceiling) → boost."""
    # 8 signals stated 0.5, 6 hit (75%)
    signals = [(0.50, 1)] * 6 + [(0.50, 0)] * 2
    _seed(empty_db, "Pattern", signals)
    cal = cc.get_agent_calibration("Pattern", db_path=str(empty_db))
    assert cal["hit_rate"] == 0.75
    assert cal["mean_stated_conf"] == 0.5
    # 0.75 / 0.5 = 1.5 → at ceiling, no clamp needed
    assert cal["multiplier"] == pytest.approx(1.5, abs=0.01)


def test_extreme_overconfidence_clamped_to_floor(empty_db):
    """Pathological case: 90% stated, 10% hit. Raw factor 0.11; clamp to 0.5
    so a single bad streak can't take a future signal to ~0%."""
    signals = [(0.90, 1)] + [(0.90, 0)] * 9   # 1/10 hit
    _seed(empty_db, "Trendy", signals)
    cal = cc.get_agent_calibration("Trendy", db_path=str(empty_db))
    assert cal["hit_rate"] == 0.1
    # Raw 0.1/0.9 ≈ 0.111 → clamped up to floor 0.5
    assert cal["multiplier"] == cc.FACTOR_MIN


def test_extreme_underconfidence_clamped_to_ceiling(empty_db):
    """Inverse: 30% stated but 90% hit. Raw factor 3.0; clamp to 1.5."""
    signals = [(0.30, 1)] * 9 + [(0.30, 0)]   # 9/10 hit
    _seed(empty_db, "Synthesis", signals)
    cal = cc.get_agent_calibration("Synthesis", db_path=str(empty_db))
    assert cal["hit_rate"] == 0.9
    assert cal["multiplier"] == cc.FACTOR_MAX


# ─── apply_to_confidence ─────────────────────────────────────────────────────


def test_apply_clamps_to_unit_range(empty_db):
    """Effective confidence must stay in [0, 1] regardless of inputs."""
    # Underconfident agent at ceiling, then feed a 0.99 signal
    signals = [(0.30, 1)] * 9 + [(0.30, 0)]
    _seed(empty_db, "Synthesis", signals)
    eff, _ = cc.apply_to_confidence(0.99, "Synthesis", db_path=str(empty_db))
    # 0.99 * 1.5 = 1.485 → clipped to 1.0
    assert eff == 1.0

    # And the floor side
    signals2 = [(0.90, 1)] + [(0.90, 0)] * 9
    _seed(empty_db, "Trendy", signals2)
    eff2, _ = cc.apply_to_confidence(0.05, "Trendy", db_path=str(empty_db))
    # 0.05 * 0.5 = 0.025 — still positive, no floor clip needed
    assert eff2 == pytest.approx(0.025)


def test_apply_returns_metadata(empty_db):
    """Callers need cal_meta to render 'stated 80% / cal 0.50 / on n=10'."""
    signals = [(0.80, 1)] * 5 + [(0.80, 0)] * 5
    _seed(empty_db, "Futurist", signals)
    eff, meta = cc.apply_to_confidence(0.80, "Futurist", db_path=str(empty_db))
    assert "multiplier" in meta
    assert "sample_size" in meta
    assert "cold_start" in meta
    assert "hit_rate" in meta
    assert meta["sample_size"] == 10
    assert meta["cold_start"] is False


# ─── lookback isolates recent regime ─────────────────────────────────────────


def test_lookback_window_excludes_old_signals(empty_db):
    """Signals older than lookback window must not pollute current calibration.
    Tests the WHERE validated_at > datetime('now', '-N days') clause."""
    conn = sqlite3.connect(empty_db)
    # Insert old (60 days ago) wrong predictions
    for i in range(20):
        sid = f"old-{i}"
        conn.execute(
            "INSERT INTO signal_alerts (id, agent_name, confidence, timestamp) "
            "VALUES (?, 'Futurist', 0.80, datetime('now', '-60 days'))",
            (sid,),
        )
        conn.execute(
            "INSERT INTO signal_scores (signal_id, agent_name, validated_at, "
            " directional_hit, brier_term) "
            "VALUES (?, 'Futurist', datetime('now', '-60 days'), 0, 0.1)",
            (sid,),
        )
    # And recent (today) accurate ones
    for i in range(10):
        sid = f"new-{i}"
        conn.execute(
            "INSERT INTO signal_alerts (id, agent_name, confidence, timestamp) "
            "VALUES (?, 'Futurist', 0.80, datetime('now'))",
            (sid,),
        )
        conn.execute(
            "INSERT INTO signal_scores (signal_id, agent_name, validated_at, "
            " directional_hit, brier_term) "
            "VALUES (?, 'Futurist', datetime('now'), 1, 0.1)",
            (sid,),
        )
    conn.commit()
    conn.close()

    cal = cc.get_agent_calibration("Futurist", db_path=str(empty_db),
                                   lookback_days=30)
    # Only the 10 recent (all hits) should count
    assert cal["sample_size"] == 10
    assert cal["hit_rate"] == 1.0


# ─── explain helper ──────────────────────────────────────────────────────────


def test_explain_signals_cold_start(empty_db):
    """Human-readable output flags uncalibrated agents so /standup is honest."""
    s = cc.explain("Newbie", 0.80, db_path=str(empty_db))
    assert "uncalibrated" in s.lower()
    assert "Newbie" in s


def test_explain_shows_calibration_when_warm(empty_db):
    signals = [(0.80, 1)] * 5 + [(0.80, 0)] * 5
    _seed(empty_db, "Futurist", signals)
    s = cc.explain("Futurist", 0.80, db_path=str(empty_db))
    assert "calibrated" not in s.lower() or "uncalibrated" not in s.lower()
    # 0.5 hit / 0.8 mean stated = 0.625 multiplier; 0.8 × 0.625 = 0.50 effective
    assert "0.62" in s
    assert "50% effective" in s
    assert "n=10" in s
