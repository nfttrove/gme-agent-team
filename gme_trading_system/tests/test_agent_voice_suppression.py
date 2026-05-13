"""Tests for the noise-reduction suppression rules in agent_voice.

Two suppression paths exercised:
  B) Chatty echoes recent Synthesis (same direction within 60s) → skip send
  C) Newsie zero-score repeats (current 0.0 and prev 0.0 within 60min) → skip
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_voice import _chatty_echoes_synthesis, _newsie_zero_score_repeat  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _offset_iso(seconds: int) -> str:
    """Return ISO timestamp `seconds` seconds in the past."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def conn(tmp_path):
    """Fresh sqlite with the agent_logs schema this code reads."""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE agent_logs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT,"
        " agent_name TEXT,"
        " task_type TEXT,"
        " status TEXT,"
        " content TEXT)"
    )
    return db


def _seed(conn, agent: str, task_type: str, content: str, ts: str | None = None,
          status: str = "ok") -> int:
    """Insert a row, return its id."""
    cur = conn.execute(
        "INSERT INTO agent_logs (timestamp, agent_name, task_type, status, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts or _now_iso(), agent, task_type, status, content),
    )
    return cur.lastrowid


# ── B: Chatty echo suppression ──────────────────────────────────────────────


class TestChattyEchoSuppression:
    """Suppress Chatty when its bias matches the most recent Synthesis brief
    emitted within the last 60 seconds."""

    def test_no_recent_synthesis_does_not_suppress(self, conn):
        """When no Synthesis brief in the window, Chatty is forwarded."""
        chatty_id = _seed(conn, "Chatty", "commentary", "team sees BEARISH")
        result = _chatty_echoes_synthesis(conn, chatty_id, "team sees BEARISH", _now_iso())
        assert result is None

    def test_matching_synthesis_suppresses(self, conn):
        """Synthesis emitted BEARISH 30s ago + Chatty says BEARISH → suppress."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22 falling | DATA: clean | STRUCTURAL: YELLOW\n"
              "NEXT: CONSENSUS: BEARISH 67% | TREND: DOWN 0.6 | PREDICTION: BEARISH 0.6\n"
              "SIGNAL: WAIT — bearish trend",
              ts=_offset_iso(30))
        chatty_id = _seed(conn, "Chatty", "commentary", "team sees BEARISH")
        result = _chatty_echoes_synthesis(conn, chatty_id, "team sees BEARISH", _now_iso())
        assert result == "BEARISH"

    def test_mismatched_direction_does_not_suppress(self, conn):
        """Synthesis BEARISH but Chatty BULLISH → let through (disagreement is signal)."""
        _seed(conn, "Synthesis", "synthesis",
              "NEXT: CONSENSUS: BEARISH 67% | TREND: DOWN 0.6 | PREDICTION: BEARISH 0.6",
              ts=_offset_iso(30))
        chatty_id = _seed(conn, "Chatty", "commentary", "team sees BULLISH")
        result = _chatty_echoes_synthesis(conn, chatty_id, "team sees BULLISH", _now_iso())
        assert result is None

    def test_synthesis_outside_window_does_not_suppress(self, conn):
        """Synthesis 5 min ago is too old — Chatty should forward."""
        _seed(conn, "Synthesis", "synthesis",
              "NEXT: CONSENSUS: BEARISH 67% | TREND: DOWN 0.6 | PREDICTION: BEARISH 0.6",
              ts=_offset_iso(300))
        chatty_id = _seed(conn, "Chatty", "commentary", "team sees BEARISH")
        result = _chatty_echoes_synthesis(conn, chatty_id, "team sees BEARISH", _now_iso())
        assert result is None

    def test_rising_maps_to_bullish(self, conn):
        """Chatty's `team sees RISING` should match Synthesis's BULLISH."""
        _seed(conn, "Synthesis", "synthesis",
              "NEXT: CONSENSUS: BULLISH 65% | TREND: UP 0.7 | PREDICTION: BULLISH 0.7",
              ts=_offset_iso(20))
        chatty_id = _seed(conn, "Chatty", "commentary", "$22.61 RISING, team sees RISING")
        result = _chatty_echoes_synthesis(conn, chatty_id, "$22.61 RISING, team sees RISING", _now_iso())
        assert result == "BULLISH"

    def test_falling_maps_to_bearish(self, conn):
        """Chatty's `FALLING` should match Synthesis's BEARISH."""
        _seed(conn, "Synthesis", "synthesis",
              "NEXT: CONSENSUS: BEARISH 60% | TREND: DOWN 0.5 | PREDICTION: BEARISH 0.55",
              ts=_offset_iso(20))
        chatty_id = _seed(conn, "Chatty", "commentary", "$22.16 FALLING on quiet vol")
        result = _chatty_echoes_synthesis(conn, chatty_id, "$22.16 FALLING on quiet vol", _now_iso())
        assert result == "BEARISH"


# ── C: Newsie zero-score repeat suppression ─────────────────────────────────


class TestNewsieZeroScoreRepeat:
    """Suppress Newsie when both current and previous row are zero-sentiment."""

    def test_current_nonzero_lets_through(self, conn):
        """Current score is non-zero → forward regardless of prior."""
        _seed(conn, "Newsie", "news", "composite=+0.00 (neutral) · 15 articles",
              ts=_offset_iso(600))
        newsie_id = _seed(conn, "Newsie", "news",
                          "composite=+0.45 (bullish) · 20 articles")
        result = _newsie_zero_score_repeat(
            conn, newsie_id, "composite=+0.45 (bullish) · 20 articles"
        )
        assert result is False

    def test_zero_followed_by_zero_suppressed(self, conn):
        """Current 0.0 and prior 0.0 within window → suppress."""
        _seed(conn, "Newsie", "news", "composite=+0.00 (neutral) · 12 articles",
              ts=_offset_iso(600))
        newsie_id = _seed(conn, "Newsie", "news",
                          "composite=+0.00 (neutral) · 14 articles")
        result = _newsie_zero_score_repeat(
            conn, newsie_id, "composite=+0.00 (neutral) · 14 articles"
        )
        assert result is True

    def test_zero_after_nonzero_lets_through(self, conn):
        """Current is 0.0 but prior was non-zero → forward (this is news)."""
        _seed(conn, "Newsie", "news", "composite=+0.45 (bullish) · 20 articles",
              ts=_offset_iso(600))
        newsie_id = _seed(conn, "Newsie", "news",
                          "composite=+0.00 (neutral) · 11 articles")
        result = _newsie_zero_score_repeat(
            conn, newsie_id, "composite=+0.00 (neutral) · 11 articles"
        )
        assert result is False

    def test_no_prior_newsie_lets_through(self, conn):
        """First Newsie ever (or first in window) → forward."""
        newsie_id = _seed(conn, "Newsie", "news",
                          "composite=+0.00 (neutral) · 5 articles")
        result = _newsie_zero_score_repeat(
            conn, newsie_id, "composite=+0.00 (neutral) · 5 articles"
        )
        assert result is False

    def test_prior_outside_window_lets_through(self, conn):
        """Prior zero from 2 hours ago is outside the 60-min window."""
        _seed(conn, "Newsie", "news", "composite=+0.00 (neutral)",
              ts=_offset_iso(2 * 3600))
        newsie_id = _seed(conn, "Newsie", "news",
                          "composite=+0.00 (neutral) · 8 articles")
        result = _newsie_zero_score_repeat(
            conn, newsie_id, "composite=+0.00 (neutral) · 8 articles"
        )
        assert result is False
