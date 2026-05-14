"""Tests for the noise-reduction suppression rules in agent_voice.

Suppression paths exercised:
  B) Chatty echoes recent Synthesis (same direction within 60s) → skip send
  C) Newsie zero-score repeats (current 0.0 and prev 0.0 within 60min) → skip
  D) Synthesis low consensus (<60% conviction is the no-information regime) → skip
  E) Synthesis state-diff (price + dir + conf unchanged within heartbeat) → skip
  F) Chatty state-diff (price unchanged, no alarm tokens, within heartbeat) → skip
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_voice import (  # noqa: E402
    _chatty_echoes_synthesis,
    _chatty_unchanged_state,
    _newsie_zero_score_repeat,
    _synthesis_low_consensus,
    _synthesis_unchanged_state,
)


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


# ── D: Synthesis low-consensus floor ────────────────────────────────────────


class TestSynthesisLowConsensusFloor:
    """Drop bursts whose CONSENSUS confidence is below 60% — the no-info regime."""

    def test_neutral_50_suppressed(self):
        content = "NOW: PRICE: $22 | NEXT: CONSENSUS: NEUTRAL 50% | SIGNAL: WAIT"
        assert _synthesis_low_consensus(content) is True

    def test_neutral_0_suppressed(self):
        content = "NOW: PRICE: $22 | NEXT: CONSENSUS: NEUTRAL 0% | SIGNAL: WAIT"
        assert _synthesis_low_consensus(content) is True

    def test_bullish_75_passes(self):
        content = "NOW: PRICE: $22 | NEXT: CONSENSUS: BULLISH 75% | SIGNAL: BUY"
        assert _synthesis_low_consensus(content) is False

    def test_at_floor_60_passes(self):
        """Exactly 60% is the threshold and should pass (only <60% suppressed)."""
        content = "NOW: PRICE: $22 | NEXT: CONSENSUS: BEARISH 60% | SIGNAL: WAIT"
        assert _synthesis_low_consensus(content) is False

    def test_no_consensus_line_does_not_suppress(self):
        """Malformed brief without CONSENSUS line → let it through (don't silently drop)."""
        assert _synthesis_low_consensus("NOW: PRICE: $22 | SIGNAL: WAIT") is False

    def test_custom_threshold(self):
        content = "NOW: NEXT: CONSENSUS: BULLISH 65% | SIGNAL: BUY"
        assert _synthesis_low_consensus(content, min_pct=70) is True
        assert _synthesis_low_consensus(content, min_pct=60) is False


# ── E: Synthesis state-diff suppression ─────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TestSynthesisUnchangedState:
    """Suppress Synthesis when price+dir+conf are within tolerance of the prior
    brief AND we've pushed within the heartbeat window."""

    def test_identical_state_recent_push_suppressed(self, conn):
        """Same price ($22.11), same dir (BEARISH), same conf (67%), pushed 5 min ago."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur_content = "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed) is True

    def test_price_moved_more_than_tolerance_passes(self, conn):
        """Price moved 1% (>0.5% default tolerance) → forward."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.33 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur_content = "NOW: PRICE: $22.33 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed) is False

    def test_consensus_flip_passes(self, conn):
        """Direction changed BEARISH → BULLISH — always forward (this IS signal)."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BULLISH 67% | SIGNAL: BUY")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur_content = "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BULLISH 67% | SIGNAL: BUY"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed) is False

    def test_confidence_jump_passes(self, conn):
        """Confidence moved from 60% → 80% (>10pp) — strengthening signal, forward."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 60% | SIGNAL: WAIT")
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 80% | SIGNAL: WAIT")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur_content = "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 80% | SIGNAL: WAIT"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed) is False

    def test_heartbeat_after_long_silence_passes(self, conn):
        """Identical state but >30 min since last push → fire so user sees agent is alive."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        last_pushed = _utc_now() - timedelta(minutes=45)
        cur_content = "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed) is False

    def test_no_prior_push_does_not_suppress(self, conn):
        """First push ever → never suppress."""
        _seed(conn, "Synthesis", "synthesis",
              "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        cur_content = "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed_at=None) is False

    def test_no_prior_synthesis_row_does_not_suppress(self, conn):
        """No previous Synthesis to compare against → forward."""
        cur_id = _seed(conn, "Synthesis", "synthesis",
                       "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur_content = "NOW: PRICE: $22.11 | NEXT: CONSENSUS: BEARISH 67% | SIGNAL: WAIT"
        assert _synthesis_unchanged_state(conn, cur_id, cur_content, last_pushed) is False


# ── F: Chatty state-diff suppression ────────────────────────────────────────


class TestChattyUnchangedState:
    """Suppress Chatty when price hasn't moved AND prose has no alarm tokens
    AND last push was inside heartbeat window."""

    def test_price_unchanged_recent_push_suppressed(self, conn):
        _seed(conn, "Chatty", "commentary", "$22.11 sideways, range $22.00-$22.20")
        cur_id = _seed(conn, "Chatty", "commentary",
                       "$22.11 holds, range $22.00-$22.20, quiet volume")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur = "$22.11 holds, range $22.00-$22.20, quiet volume"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed) is True

    def test_alarm_word_passes(self, conn):
        """Prose contains FALLING — material change, forward even if price unchanged."""
        _seed(conn, "Chatty", "commentary", "$22.11 sideways quiet volume")
        cur_id = _seed(conn, "Chatty", "commentary",
                       "$22.11 FALLING on elevated volume")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur = "$22.11 FALLING on elevated volume"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed) is False

    def test_volume_spike_word_passes(self, conn):
        """SPIKE in volume → material, forward."""
        _seed(conn, "Chatty", "commentary", "$22.11 quiet volume sideways")
        cur_id = _seed(conn, "Chatty", "commentary",
                       "$22.11 spike in volume now $22.13")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur = "$22.11 spike in volume now $22.13"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed) is False

    def test_price_move_above_tolerance_passes(self, conn):
        """0.6% price move > 0.5% tolerance → forward."""
        _seed(conn, "Chatty", "commentary", "$22.00 sideways quiet volume")
        cur_id = _seed(conn, "Chatty", "commentary",
                       "$22.15 holds steady, quiet volume")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur = "$22.15 holds steady, quiet volume"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed) is False

    def test_heartbeat_after_long_silence_passes(self, conn):
        """Same price, no alarm, but 45 min since last push → fire."""
        _seed(conn, "Chatty", "commentary", "$22.11 sideways quiet volume")
        cur_id = _seed(conn, "Chatty", "commentary",
                       "$22.11 sideways quiet volume")
        last_pushed = _utc_now() - timedelta(minutes=45)
        cur = "$22.11 sideways quiet volume"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed) is False

    def test_no_prior_push_does_not_suppress(self, conn):
        """Never pushed before → forward."""
        _seed(conn, "Chatty", "commentary", "$22.11 sideways")
        cur_id = _seed(conn, "Chatty", "commentary", "$22.11 sideways still")
        cur = "$22.11 sideways still"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed_at=None) is False

    def test_no_prior_chatty_row_does_not_suppress(self, conn):
        cur_id = _seed(conn, "Chatty", "commentary", "$22.11 sideways")
        last_pushed = _utc_now() - timedelta(minutes=5)
        cur = "$22.11 sideways"
        assert _chatty_unchanged_state(conn, cur_id, cur, last_pushed) is False
