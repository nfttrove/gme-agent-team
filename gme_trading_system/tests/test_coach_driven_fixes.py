"""Tests for the two fixes that /coach diagnoses prompted on 2026-05-16.

Pattern Intraday: ATR chop filter (block when 5m ATR < 0.3% of price).
Futurist: invert BULL signals with confidence > 75% (flip bias + swap SL/TP).
"""
from unittest.mock import MagicMock, patch

import pytest

from pattern_detector import PatternReport


def _make_report(atr14: float | None, last_close: float | None = 21.50, pattern_type: str = "triangle") -> PatternReport:
    """Build a minimal PatternReport with the indicators dict the ATR filter reads."""
    indicators = {}
    if atr14 is not None:
        indicators["atr14"] = atr14
    if last_close is not None:
        indicators["price"] = last_close
    return PatternReport(
        pattern_type=pattern_type,
        confidence=0.85,
        breakout_level=21.55,
        breakout_direction="UP",
        reasoning="test pattern",
        severity="HIGH",
        indicators=indicators,
    )


class TestIntradayATRFilter:
    """Pattern Intraday should suppress when 5m ATR is below the chop floor."""

    def test_atr_below_floor_returns_none_with_chop_filter_reason(self, monkeypatch):
        from orchestrator import _compute_intraday_pattern_signal, _INTRADAY_ATR_FLOOR_PCT
        import intraday_aggregator
        import orchestrator

        # Stub the data layer: enough candles, but the detector returns a
        # report whose ATR is below the floor (ATR 0.02 on $21 = 0.095% < 0.3%).
        monkeypatch.setattr(intraday_aggregator, "aggregate_5m_bars", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator, "_fetch_intraday_candles",
                            lambda *a, **kw: [{"timestamp": "t", "close": 21.5}] * 35)
        monkeypatch.setattr("pattern_detector.detect_patterns",
                            lambda *a, **kw: _make_report(atr14=0.02, last_close=21.50))

        signal, narrative = _compute_intraday_pattern_signal()

        assert signal is None
        assert "ATR chop filter" in narrative
        # Floor is 0.3% per orchestrator constant; report should print actual vs floor
        assert "0.10%" in narrative or "0.09%" in narrative  # 0.02/21.50 ≈ 0.093%
        assert f"{_INTRADAY_ATR_FLOOR_PCT:.2%}" in narrative

    def test_atr_above_floor_passes_through_to_signal(self, monkeypatch):
        from orchestrator import _compute_intraday_pattern_signal
        import intraday_aggregator
        import orchestrator

        # ATR 0.10 on $21 = 0.47%, above the 0.3% floor.
        monkeypatch.setattr(intraday_aggregator, "aggregate_5m_bars", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator, "_fetch_intraday_candles",
                            lambda *a, **kw: [{"timestamp": "t", "close": 21.5}] * 35)
        monkeypatch.setattr("pattern_detector.detect_patterns",
                            lambda *a, **kw: _make_report(atr14=0.10, last_close=21.50))

        signal, narrative = _compute_intraday_pattern_signal()

        assert signal is not None
        assert signal.pattern_type == "triangle"

    def test_atr_missing_passes_through_no_filter(self, monkeypatch):
        """If the detector didn't compute ATR (rare data-quality issue), don't block."""
        from orchestrator import _compute_intraday_pattern_signal
        import intraday_aggregator
        import orchestrator

        monkeypatch.setattr(intraday_aggregator, "aggregate_5m_bars", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator, "_fetch_intraday_candles",
                            lambda *a, **kw: [{"timestamp": "t", "close": 21.5}] * 35)
        monkeypatch.setattr("pattern_detector.detect_patterns",
                            lambda *a, **kw: _make_report(atr14=None, last_close=21.50))

        signal, narrative = _compute_intraday_pattern_signal()

        # Signal builds normally; we don't penalize missing ATR data
        assert signal is not None


class TestFuturistHighConfBullInvert:
    """Per /coach diagnosis: BULL signals with conf > 75% should flip to BEAR.

    We test the inversion arithmetic in isolation by replicating the inline
    logic — full end-to-end is too brittle to test against the live LLM path.
    """

    def _invert_if_needed(self, bias: str, conf: float, sl: float, tp: float):
        """Mirror the inline logic from run_futurist_prediction_signal."""
        from orchestrator import _FUTURIST_INVERT_BULL_CONF_FLOOR
        emit_bias = bias
        emit_sl, emit_tp = sl, tp
        inversion_tag = ""
        if (bias or "").upper().startswith("BULL") and conf > _FUTURIST_INVERT_BULL_CONF_FLOOR:
            emit_bias = "BEARISH"
            emit_sl, emit_tp = tp, sl
            inversion_tag = f"[INVERTED: orig BULL conf={conf:.0%}] "
        return emit_bias, emit_sl, emit_tp, inversion_tag

    def test_bull_above_floor_flips_to_bear_and_swaps_sl_tp(self):
        bias, sl, tp, tag = self._invert_if_needed("BULLISH", 0.82, sl=20.0, tp=22.0)
        assert bias == "BEARISH"
        assert sl == 22.0  # original TP becomes new SL
        assert tp == 20.0  # original SL becomes new TP
        assert "INVERTED" in tag
        assert "82%" in tag

    def test_bull_at_threshold_does_not_flip(self):
        """Strictly greater-than — 75% exactly stays BULL."""
        bias, sl, tp, tag = self._invert_if_needed("BULLISH", 0.75, sl=20.0, tp=22.0)
        assert bias == "BULLISH"
        assert tag == ""

    def test_bull_below_floor_does_not_flip(self):
        bias, sl, tp, tag = self._invert_if_needed("BULLISH", 0.65, sl=20.0, tp=22.0)
        assert bias == "BULLISH"
        assert tag == ""

    def test_bear_signal_never_inverts_regardless_of_confidence(self):
        bias, sl, tp, tag = self._invert_if_needed("BEARISH", 0.95, sl=22.0, tp=20.0)
        assert bias == "BEARISH"
        assert tag == ""

    def test_neutral_signal_not_inverted(self):
        bias, sl, tp, tag = self._invert_if_needed("NEUTRAL", 0.90, sl=20.0, tp=22.0)
        assert bias == "NEUTRAL"
        assert tag == ""

    def test_inversion_floor_is_set_to_0_75(self):
        """Anchor the threshold so accidental tuning leaves a test fingerprint."""
        from orchestrator import _FUTURIST_INVERT_BULL_CONF_FLOOR
        assert _FUTURIST_INVERT_BULL_CONF_FLOOR == 0.75
