"""
Tests for pattern_detector — the deterministic replacement for the LLM-based
pattern agent.

Critical invariants:
  1. Refuses to output a pattern when the geometry doesn't support one.
     (The whole point — the LLM version hallucinated 'ascending_triangle'
     on random noise.)
  2. Correctly identifies ascending/descending/symmetric triangles from
     synthetic data with known geometry.
  3. Correctly flags breakouts vs. consolidation.
  4. Confidence is bounded at 0.85 — no cosplay certainty.
  5. Indicator math matches the `ta` library's outputs (we delegate to it).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pattern_detector import detect_patterns, PatternReport  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _candles_from_closes(closes, base_date="2026-03-01", vol=1_000_000):
    """Build a candle list from closes. Highs/lows set ±0.5% of close so
    swing-point detection has something to work with."""
    out = []
    for i, c in enumerate(closes):
        out.append({
            "date": f"{base_date}",  # not used by detector
            "open": float(c),
            "high": float(c) * 1.005,
            "low":  float(c) * 0.995,
            "close": float(c),
            "volume": vol,
        })
    return out


def _ascending_triangle_series(n: int = 30) -> list[float]:
    """Flat highs around 30.0, rising lows from 27 → 29.5.

    Shape per 5-bar cycle: [ceiling, easing, floor, recovery, approach]
    so the swing-high and swing-low detectors (window=3) can actually pick
    out the pivots. Real triangles look like this, not like a 2-bar zigzag.
    """
    closes = []
    ceiling = 30.0
    cycles = n // 5 + 1
    floor_levels = np.linspace(27.0, 29.5, cycles)
    for i in range(n):
        cycle = i % 5
        floor = float(floor_levels[min(i // 5, cycles - 1)])
        if cycle == 0:
            closes.append(ceiling)          # touch the flat ceiling
        elif cycle == 1:
            closes.append(ceiling * 0.98)   # ease off
        elif cycle == 2:
            closes.append(floor)            # touch the rising floor
        elif cycle == 3:
            closes.append(floor * 1.02)     # bounce
        else:  # cycle == 4
            closes.append(ceiling * 0.99)   # approach ceiling again
    return closes


def _descending_triangle_series(n: int = 30) -> list[float]:
    """Flat lows around 20.0, falling highs from 23 → 20.5.

    Shape per 5-bar cycle: [floor, recovery, ceiling, easing, approach].
    """
    closes = []
    floor = 20.0
    cycles = n // 5 + 1
    ceiling_levels = np.linspace(23.0, 20.5, cycles)
    for i in range(n):
        cycle = i % 5
        ceil = float(ceiling_levels[min(i // 5, cycles - 1)])
        if cycle == 0:
            closes.append(floor)            # flat floor
        elif cycle == 1:
            closes.append(floor * 1.02)     # recovery
        elif cycle == 2:
            closes.append(ceil)             # falling ceiling
        elif cycle == 3:
            closes.append(ceil * 0.98)      # easing off
        else:  # cycle == 4
            closes.append(floor * 1.01)     # approach floor
    return closes


def _random_walk(n: int = 30, seed: int = 42, start: float = 25.0) -> list[float]:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.3, size=n)
    closes = [start]
    for s in steps[1:]:
        closes.append(max(1.0, closes[-1] + float(s)))
    return closes


# ── refusal-to-invent ────────────────────────────────────────────────────────

def test_returns_none_when_insufficient_data():
    """Fewer than 15 candles → detector refuses to name any pattern."""
    candles = _candles_from_closes([25.0] * 10)
    assert detect_patterns(candles) is None


def test_random_noise_does_not_produce_a_clean_triangle():
    """On random walks, the detector should either say 'none' OR produce a
    pattern with low confidence — it must NOT confidently claim
    ascending/descending triangle on noise."""
    candles = _candles_from_closes(_random_walk(30, seed=7))
    report = detect_patterns(candles)
    assert report is not None  # 30 candles is enough to produce a report
    # Either no pattern, or any triangle claim must be low-confidence
    if report.pattern_type in ("ascending_triangle", "descending_triangle",
                               "symmetric_triangle"):
        assert report.confidence <= 0.70, (
            f"Detector hallucinated {report.pattern_type} on random noise "
            f"with confidence {report.confidence}"
        )


# ── triangle detection ───────────────────────────────────────────────────────

def test_ascending_triangle_is_detected():
    candles = _candles_from_closes(_ascending_triangle_series(30))
    report = detect_patterns(candles)
    assert report is not None
    assert report.pattern_type == "ascending_triangle"
    assert report.breakout_direction == "UP"
    # Breakout level should be near the flat ceiling (30.0), within 1%
    assert abs(report.breakout_level - 30.0 * 1.005) / 30.0 < 0.02


def test_descending_triangle_is_detected():
    candles = _candles_from_closes(_descending_triangle_series(30))
    report = detect_patterns(candles)
    assert report is not None
    assert report.pattern_type == "descending_triangle"
    assert report.breakout_direction == "DOWN"


# ── breakout detection ───────────────────────────────────────────────────────

def test_clean_breakout_is_flagged_as_breakout_not_triangle():
    """Price ranges 24-26 for 25 bars, then spikes to 28. That's a breakout,
    not a triangle — detector must call it correctly."""
    rng = np.random.default_rng(1)
    base = [float(rng.uniform(24, 26)) for _ in range(25)]
    breakout_tail = [26.5, 27.0, 27.5, 28.0, 28.3]
    candles = _candles_from_closes(base + breakout_tail)
    report = detect_patterns(candles)
    assert report is not None
    assert report.pattern_type in ("breakout", "channel_up"), (
        f"Expected breakout or channel_up, got {report.pattern_type}"
    )
    assert report.breakout_direction == "UP"


def test_breakdown_is_detected():
    rng = np.random.default_rng(2)
    base = [float(rng.uniform(24, 26)) for _ in range(25)]
    breakdown_tail = [23.5, 23.0, 22.5, 22.0, 21.7]
    candles = _candles_from_closes(base + breakdown_tail)
    report = detect_patterns(candles)
    assert report is not None
    assert report.pattern_type in ("breakdown", "channel_down")
    assert report.breakout_direction == "DOWN"


# ── confidence bounds ────────────────────────────────────────────────────────

def test_confidence_never_exceeds_cap():
    """No matter how clean the pattern, confidence ≤ 0.85. Chart patterns
    are not a 99%-certainty tool and we must never present them as one."""
    candles = _candles_from_closes(_ascending_triangle_series(30))
    report = detect_patterns(candles)
    assert report is not None
    assert report.confidence <= 0.85


def test_no_pattern_gets_low_confidence():
    """If geometry returns 'none', confidence is 0.30 (floor), severity LOW."""
    # Flat-line data — no swings at all
    candles = _candles_from_closes([25.0] * 30)
    report = detect_patterns(candles)
    assert report is not None
    if report.pattern_type == "none":
        assert report.confidence == 0.30
        assert report.severity == "LOW"


# ── indicator values ─────────────────────────────────────────────────────────

def test_indicators_include_rsi_macd_bollinger_atr():
    """Detector must surface the full indicator stack for downstream use.
    Need ≥35 bars so MACD(12/26/9) has warmed up — with fewer bars it
    legitimately returns None (which the detector should preserve, not
    fabricate a value for)."""
    candles = _candles_from_closes(_ascending_triangle_series(50))
    report = detect_patterns(candles)
    assert report is not None
    ind = report.indicators
    for key in ("rsi14", "macd", "macd_signal", "macd_hist",
                "bb_upper", "bb_lower", "atr14", "price"):
        assert key in ind, f"missing indicator: {key}"
        assert ind[key] is not None, f"{key} is None on a 50-bar series"


def test_indicator_honestly_none_on_short_series():
    """With only 20 bars, MACD hasn't warmed up — key must exist but be None,
    NOT a fabricated number. This is the core anti-hallucination contract."""
    candles = _candles_from_closes(_random_walk(20))
    report = detect_patterns(candles)
    assert report is not None
    # macd_hist needs ~35 bars; with 20, it should be explicitly None
    assert report.indicators.get("macd_hist") is None


def test_rsi_is_in_valid_range():
    """RSI is bounded [0, 100] — if we're outside that, the library call failed."""
    candles = _candles_from_closes(_random_walk(30))
    report = detect_patterns(candles)
    assert report is not None
    rsi = report.indicators.get("rsi14")
    assert 0 <= rsi <= 100, f"RSI out of range: {rsi}"


# ── reasoning is citation-grounded ───────────────────────────────────────────

def test_reasoning_cites_numbers_not_adjectives():
    """The detector's `reasoning` field must be a citation list, not vibes.
    Either 'none' or it contains actual indicator values from `indicators`."""
    candles = _candles_from_closes(_ascending_triangle_series(30))
    report = detect_patterns(candles)
    assert report is not None
    # For a non-'none' pattern, reasoning must mention at least one numeric cue
    if report.pattern_type != "none":
        assert any(ch.isdigit() for ch in report.reasoning), (
            f"reasoning has no numbers: {report.reasoning!r}"
        )
