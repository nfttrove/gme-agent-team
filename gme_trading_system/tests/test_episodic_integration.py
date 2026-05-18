"""
Tests for the Futurist episodic-logging hook that the orchestrator calls
after each prediction, mirroring the Synthesis wiring.

Why this matters: this hook was orphaned for weeks (RUNBOOK item #8).
Wiring it without tests would risk silent regressions when the orchestrator's
prediction payload drifts. The orchestrator's try/except catches exceptions
so production stays up, but test coverage is the only thing that flags a
broken hook.

The orchestrator calls `episodic_logger.log_prediction` directly with
the validated Pydantic prediction's fields (it intentionally bypasses
the wrapper in episodic_integration.py whose regex extractor expects an
older nested JSON shape that no longer matches `raw`). These tests
verify that direct path against the orchestrator's payload shape.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `.agent/` lives alongside `gme_trading_system/`; episodic_logger isn't
# importable from gme_trading_system's sys.path by default.
import os
AGENT_DIR = os.path.normpath(os.path.join(REPO_ROOT, "..", ".agent"))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

import episodic_logger  # noqa: E402


class TestLogPredictionFromOrchestratorPayload:
    """The orchestrator passes pre-validated Pydantic prediction fields
    straight into log_prediction. Verify the function accepts that shape
    and produces an episode id."""

    def test_canonical_payload_writes_episode(self, tmp_path, monkeypatch):
        """Given the field set the orchestrator passes (str agent_name,
        float prices, str horizon ending in m/h/d/w, str bias), When
        log_prediction is called, Then it returns a non-empty episode id."""
        # Redirect _append_episode's file write to a tmp dir so the test
        # doesn't touch the real episodic store.
        with patch("episodic_logger._append_episode", return_value="ep_test_123") as mock_append:
            episode_id = episodic_logger.log_prediction(
                agent_name="Futurist",
                predicted_price=21.95,
                confidence=0.55,
                horizon="1h",
                bias="BEARISH",
                reasoning="Price below VWAP and EMA21, RSI low",
            )
        assert episode_id == "ep_test_123"
        assert mock_append.called
        episode = mock_append.call_args.args[0]
        # Check the dict shape that gets persisted
        assert episode["agent"] == "Futurist"
        assert episode["predicted_price"] == 21.95
        assert episode["confidence"] == 0.55
        assert episode["horizon"] == "1h"
        assert episode["bias"] == "BEARISH"
        assert "VWAP" in episode["reasoning"]
        assert episode["type"] == "prediction"

    def test_orchestrator_try_except_swallows_disk_full(self):
        """Given the disk-write raises (full disk, perm denied, etc.),
        When the orchestrator's try/except wrapper runs the hook, Then
        the exception is caught at the call site — protecting the signal
        emit path. The hook itself raises (we verify); the orchestrator's
        wrapper is what swallows it (covered by the import-and-call
        pattern at orchestrator.py:787-799)."""
        with patch("episodic_logger._append_episode", side_effect=OSError("disk full")):
            try:
                episodic_logger.log_prediction(
                    agent_name="Futurist",
                    predicted_price=21.95,
                    confidence=0.55,
                    horizon="1h",
                    bias="BEARISH",
                    reasoning="weak",
                )
            except OSError as e:
                # Hook raised as expected — the orchestrator's outer
                # try/except is responsible for swallowing this.
                assert "disk full" in str(e)
            else:
                raise AssertionError("expected OSError to propagate from the hook")
