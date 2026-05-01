"""
Deterministic chart-pattern detection — no LLM, no vibes.

Replaces the old `_compute_pattern_signal` which handed raw OHLCV to Gemma
and asked it to "detect patterns". That was theatre: the LLM was copying
previous logs and producing the same "ascending_triangle @ $26.40 (68%)"
verbatim every 2 hours regardless of what the chart actually did.

Design principles:
  - Every number is derived from price data via a tested formula.
  - The detector refuses to name a pattern if the geometric criteria aren't
    met — no "looks kind of like a triangle" fudge.
  - Confidence is a function of how many independent indicators confirm,
    NOT the LLM's self-rated certainty.
  - Gemma's only job (if available at all) is to turn the structured output
    into a plain-English sentence. It never decides what the pattern IS.

Indicators:
  - RSI14, MACD (12/26/9), Bollinger (20, 2σ), ATR14 — via `ta` library
  - Swing highs/lows — via rolling local-max/min
  - Triangle detection — linear regression on swing points

Outputs a PatternReport with pattern_type, breakout_level, breakout_direction,
confidence, severity, reasoning (citation-grounded). Shape matches the
existing PatternSignal Pydantic model so downstream code is unchanged.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict, field
from typing import Optional, Sequence

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── tuning constants (conservative — err toward "no pattern") ────────────────

MIN_CANDLES = 15
SWING_WINDOW = 3          # how many bars on each side to qualify as swing high/low
MIN_SWINGS_FOR_FIT = 3    # need ≥3 swings to call a regression line meaningful
# Flatness is defined relative to the window's price range, not ATR.
# A slope is "flat" if, extrapolated over the window, it moves less than
# FLATNESS_FRACTION of the full high-low range.
FLATNESS_FRACTION = 0.15
CONVERGENCE_MIN_DELTA = 0.3  # min fractional narrowing between first/last bar of window
BREAKOUT_TAIL = 5         # bars at the end excluded when computing base range
                          # (a "breakout" by definition means price left the base)


# ── schema ───────────────────────────────────────────────────────────────────

@dataclass
class PatternReport:
    pattern_type: str          # ascending_triangle | descending_triangle | ...
    confidence: float          # 0.0 – 1.0, derived from # of confirming signals
    breakout_level: float      # the level the detector thinks price needs to break
    breakout_direction: str    # "UP" or "DOWN"
    reasoning: str             # plain-English, citation-grounded, under 220 chars
    severity: str              # "HIGH" | "MEDIUM" | "LOW"
    indicators: dict = field(default_factory=dict)  # raw indicator values

    def as_dict(self) -> dict:
        return asdict(self)


# ── public entry point ───────────────────────────────────────────────────────

def detect_patterns(
    candles: Sequence[dict],
    config: dict | None = None,
) -> Optional[PatternReport]:
    """Analyze OHLCV candles and return a PatternReport, or None if we don't
    have enough data to say anything honest.

    Expected candle schema: {'date', 'open', 'high', 'low', 'close', 'volume'}
    in chronological order (oldest first).

    `config` (optional) overrides the module-level tuning constants — used by
    the intraday caller, which needs a higher MIN_CANDLES and is otherwise
    happy with the same relative thresholds. Keys honoured: MIN_CANDLES,
    SWING_WINDOW, FLATNESS_FRACTION, CONVERGENCE_MIN_DELTA, BREAKOUT_TAIL.
    """
    cfg = {
        "MIN_CANDLES": MIN_CANDLES,
        "SWING_WINDOW": SWING_WINDOW,
        "FLATNESS_FRACTION": FLATNESS_FRACTION,
        "CONVERGENCE_MIN_DELTA": CONVERGENCE_MIN_DELTA,
        "BREAKOUT_TAIL": BREAKOUT_TAIL,
    }
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg})

    if not candles or len(candles) < cfg["MIN_CANDLES"]:
        return None

    df = _to_dataframe(candles, cfg["MIN_CANDLES"])
    if df is None or df.empty:
        return None

    indicators = _compute_indicators(df)
    geometry = _detect_geometry(df, indicators.get("atr14"), cfg)

    # Combine indicators + geometry into a single verdict.
    return _build_report(df, indicators, geometry)


# ── private: dataframe prep ──────────────────────────────────────────────────

def _to_dataframe(candles: Sequence[dict], min_candles: int = MIN_CANDLES) -> Optional[pd.DataFrame]:
    try:
        df = pd.DataFrame(list(candles))
        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                return None
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        return df if len(df) >= min_candles else None
    except Exception as e:
        log.warning(f"[pattern_detector] dataframe build failed: {e}")
        return None


# ── private: indicator computation (via `ta` library) ────────────────────────

def _compute_indicators(df: pd.DataFrame) -> dict:
    """Return a dict of RSI, MACD, Bollinger, ATR — current-bar values only."""
    try:
        from ta.momentum import RSIIndicator
        from ta.trend import MACD
        from ta.volatility import AverageTrueRange, BollingerBands
    except ImportError:
        log.error("[pattern_detector] `ta` library not installed")
        return {}

    out: dict = {}
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]

    def _last_finite(series) -> Optional[float]:
        """Return the last non-NaN value, or None. `ta` emits NaN until it
        has enough bars for its window."""
        try:
            val = float(series.iloc[-1])
            return val if not math.isnan(val) else None
        except Exception:
            return None

    try:
        out["rsi14"] = round(v, 2) if (v := _last_finite(RSIIndicator(closes, window=14).rsi())) is not None else None
    except Exception as e:
        log.warning(f"[pattern_detector] rsi failed: {e}")

    try:
        macd = MACD(closes)
        out["macd"] = round(v, 4) if (v := _last_finite(macd.macd())) is not None else None
        out["macd_signal"] = round(v, 4) if (v := _last_finite(macd.macd_signal())) is not None else None
        out["macd_hist"] = round(v, 4) if (v := _last_finite(macd.macd_diff())) is not None else None
        # Crossover detection on the last 2 bars
        hist = macd.macd_diff()
        if len(hist) >= 2 and not math.isnan(hist.iloc[-2]) and not math.isnan(hist.iloc[-1]):
            if hist.iloc[-2] <= 0 < hist.iloc[-1]:
                out["macd_cross"] = "bullish"
            elif hist.iloc[-2] >= 0 > hist.iloc[-1]:
                out["macd_cross"] = "bearish"
            else:
                out["macd_cross"] = "none"
    except Exception as e:
        log.warning(f"[pattern_detector] macd failed: {e}")

    try:
        bb = BollingerBands(closes, window=20, window_dev=2)
        out["bb_upper"] = round(v, 2) if (v := _last_finite(bb.bollinger_hband())) is not None else None
        out["bb_lower"] = round(v, 2) if (v := _last_finite(bb.bollinger_lband())) is not None else None
        out["bb_mid"]   = round(v, 2) if (v := _last_finite(bb.bollinger_mavg())) is not None else None
        if out.get("bb_upper") is not None and out.get("bb_lower") is not None:
            out["bb_width"] = round(out["bb_upper"] - out["bb_lower"], 3)
    except Exception as e:
        log.warning(f"[pattern_detector] bollinger failed: {e}")

    try:
        atr_series = AverageTrueRange(highs, lows, closes, window=14).average_true_range()
        out["atr14"] = round(v, 3) if (v := _last_finite(atr_series)) is not None else None
    except Exception as e:
        log.warning(f"[pattern_detector] atr failed: {e}")

    out["price"] = round(float(closes.iloc[-1]), 2)
    return out


# ── private: geometric pattern detection ─────────────────────────────────────

def _find_swings(series: pd.Series, window: int, is_high: bool) -> list[tuple[int, float]]:
    """Return list of (index, value) for each local extremum — a point is a
    swing high if strictly greater than all `window` neighbours on each side,
    and a swing low if strictly lower."""
    out = []
    arr = series.values
    for i in range(window, len(arr) - window):
        neighbourhood = arr[i - window : i + window + 1]
        if is_high and arr[i] == neighbourhood.max() and arr[i] > arr[i - 1]:
            out.append((i, float(arr[i])))
        elif not is_high and arr[i] == neighbourhood.min() and arr[i] < arr[i - 1]:
            out.append((i, float(arr[i])))
    return out


def _linear_fit(points: list[tuple[int, float]]) -> Optional[tuple[float, float]]:
    """Least-squares slope + intercept. Returns (slope_per_bar, intercept) or
    None if fewer than 2 points."""
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    # numpy.polyfit deg=1
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def _detect_geometry(df: pd.DataFrame, atr: Optional[float], cfg: dict | None = None) -> dict:
    """Classify the recent shape: ascending/descending/symmetric triangle,
    channel up/down, breakout, breakdown, or none. Returns a dict with
    `pattern`, `breakout_level`, `breakout_direction`, and the swing points
    used so callers can cite them."""
    if cfg is None:
        cfg = {
            "SWING_WINDOW": SWING_WINDOW,
            "FLATNESS_FRACTION": FLATNESS_FRACTION,
            "CONVERGENCE_MIN_DELTA": CONVERGENCE_MIN_DELTA,
            "BREAKOUT_TAIL": BREAKOUT_TAIL,
        }
    swing_window = cfg.get("SWING_WINDOW", SWING_WINDOW)
    flatness_fraction = cfg.get("FLATNESS_FRACTION", FLATNESS_FRACTION)
    convergence_min_delta = cfg.get("CONVERGENCE_MIN_DELTA", CONVERGENCE_MIN_DELTA)
    breakout_tail = cfg.get("BREAKOUT_TAIL", BREAKOUT_TAIL)

    # Only consider the last ~30 bars (or all if fewer) for pattern scope
    recent = df.iloc[-min(30, len(df)):].reset_index(drop=True)

    highs = _find_swings(recent["high"], swing_window, is_high=True)
    lows = _find_swings(recent["low"], swing_window, is_high=False)

    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    last_close = float(recent["close"].iloc[-1])
    bars = len(recent)

    # Flatness tolerance scaled to the window's own range: a slope is "flat"
    # if, over the whole window, it would move less than FLATNESS_FRACTION
    # of the total high-low range. This makes flatness consistent regardless
    # of the stock's absolute price level.
    range_per_bar = max((recent_high - recent_low) / bars, 1e-4)
    flat_threshold = range_per_bar * flatness_fraction

    high_fit = _linear_fit(highs) if len(highs) >= MIN_SWINGS_FOR_FIT else None
    low_fit = _linear_fit(lows) if len(lows) >= MIN_SWINGS_FOR_FIT else None

    geometry = {
        "pattern": "none",
        "breakout_level": recent_high,
        "breakout_direction": "UP",
        "recent_high": round(recent_high, 2),
        "recent_low": round(recent_low, 2),
        "swing_highs": highs,
        "swing_lows": lows,
        "last_close": round(last_close, 2),
        "high_slope": round(high_fit[0], 4) if high_fit else None,
        "low_slope": round(low_fit[0], 4) if low_fit else None,
    }

    # Breakout / breakdown — compare current close against the BASE range
    # (the window excluding the last few bars, which may themselves be the
    # breakout move). Without this exclusion, the "high of the window"
    # includes the breakout bar itself and we'd never fire.
    tail_bars = min(breakout_tail, max(1, bars // 6))
    base = recent.iloc[:-tail_bars] if bars > tail_bars + 5 else recent
    base_high = float(base["high"].max())
    base_low = float(base["low"].min())
    # Use ATR if we have it, otherwise fall back to a fraction of the base's
    # own range so the threshold scales with the stock's volatility.
    breakout_margin = (atr or (base_high - base_low) / max(len(base), 1)) * 0.3
    if last_close > base_high + breakout_margin:
        geometry["pattern"] = "breakout"
        geometry["breakout_level"] = round(base_high, 2)
        geometry["breakout_direction"] = "UP"
        return geometry
    if last_close < base_low - breakout_margin:
        geometry["pattern"] = "breakdown"
        geometry["breakout_level"] = round(base_low, 2)
        geometry["breakout_direction"] = "DOWN"
        return geometry

    # Triangle family — needs both fits
    if high_fit and low_fit:
        hs, _ = high_fit
        ls, _ = low_fit
        high_flat = abs(hs) < flat_threshold
        low_flat = abs(ls) < flat_threshold

        # Convergence check — does the range narrow over the window?
        first_range = highs[0][1] - lows[0][1] if highs and lows else None
        last_range = highs[-1][1] - lows[-1][1] if highs and lows else None
        converging = (
            first_range is not None
            and last_range is not None
            and first_range > 0
            and (first_range - last_range) / first_range > convergence_min_delta
        )

        if high_flat and ls > flat_threshold:
            geometry["pattern"] = "ascending_triangle"
            geometry["breakout_level"] = round(np.mean([h[1] for h in highs]), 2)
            geometry["breakout_direction"] = "UP"
        elif low_flat and hs < -flat_threshold:
            geometry["pattern"] = "descending_triangle"
            geometry["breakout_level"] = round(np.mean([l[1] for l in lows]), 2)
            geometry["breakout_direction"] = "DOWN"
        elif hs < -flat_threshold and ls > flat_threshold and converging:
            geometry["pattern"] = "symmetric_triangle"
            # Breakout level = most recent swing high (bias toward the direction
            # price is closer to)
            geometry["breakout_level"] = round(highs[-1][1], 2)
            geometry["breakout_direction"] = "UP" if last_close >= (recent_high + recent_low) / 2 else "DOWN"
        elif hs > flat_threshold and ls > flat_threshold:
            geometry["pattern"] = "channel_up"
            geometry["breakout_level"] = round(highs[-1][1], 2)
            geometry["breakout_direction"] = "UP"
        elif hs < -flat_threshold and ls < -flat_threshold:
            geometry["pattern"] = "channel_down"
            geometry["breakout_level"] = round(lows[-1][1], 2)
            geometry["breakout_direction"] = "DOWN"
        elif high_flat and low_flat:
            geometry["pattern"] = "consolidation"
            geometry["breakout_level"] = round(recent_high, 2)
            geometry["breakout_direction"] = "UP" if last_close >= (recent_high + recent_low) / 2 else "DOWN"

    return geometry


# ── private: report assembly + confidence grading ────────────────────────────

def _build_report(df: pd.DataFrame, indicators: dict, geometry: dict) -> PatternReport:
    """Combine indicator + geometry verdicts into a single PatternReport.

    Confidence is derived from how many of these independent signals agree:
      - Geometry named a pattern (not 'none')
      - RSI is in the expected zone for the pattern
      - MACD histogram sign matches pattern direction
      - Price sits near a legitimate swing point (not mid-air)

    Max confirmations = 4 → max confidence 0.85. We cap at 0.85 so the
    LLM narrator downstream has no room to claim "certainty" — that's
    fundamentally not what chart patterns provide.
    """
    pattern = geometry["pattern"]
    direction = geometry["breakout_direction"]
    level = geometry["breakout_level"]
    price = indicators.get("price", geometry["last_close"])

    confirmations = 0
    cues = []

    if pattern != "none":
        confirmations += 1
        cues.append(f"geometry={pattern}")

    rsi = indicators.get("rsi14")
    if rsi is not None:
        if direction == "UP" and rsi > 50:
            confirmations += 1
            cues.append(f"RSI {rsi:.0f} > 50")
        elif direction == "DOWN" and rsi < 50:
            confirmations += 1
            cues.append(f"RSI {rsi:.0f} < 50")

    mh = indicators.get("macd_hist")
    if mh is not None:
        if direction == "UP" and mh > 0:
            confirmations += 1
            cues.append(f"MACD hist +{mh:.3f}")
        elif direction == "DOWN" and mh < 0:
            confirmations += 1
            cues.append(f"MACD hist {mh:.3f}")

    # "Near level" — within 2% of breakout
    if level and abs(price - level) / level < 0.02:
        confirmations += 1
        cues.append(f"price ${price:.2f} within 2% of level ${level:.2f}")

    # Grade: each confirmation adds 0.2, cap at 0.85. If pattern is 'none',
    # confidence is 0.30 (we have indicator context but nothing to act on).
    if pattern == "none":
        confidence = 0.30
        severity = "LOW"
    else:
        confidence = min(0.30 + 0.20 * confirmations, 0.85)
        severity = "HIGH" if confidence >= 0.70 else "MEDIUM" if confidence >= 0.50 else "LOW"

    # Reasoning: pure citation, no narrative fluff. Narration is done by the
    # caller's LLM step if it wants a sentence.
    reasoning_bits = [f"{pattern}"] + cues
    reasoning = " · ".join(reasoning_bits)[:220]

    return PatternReport(
        pattern_type=pattern,
        confidence=round(confidence, 3),
        breakout_level=float(level),
        breakout_direction=direction,
        reasoning=reasoning,
        severity=severity,
        indicators={**indicators, "swing_highs": geometry["swing_highs"],
                    "swing_lows": geometry["swing_lows"],
                    "high_slope": geometry.get("high_slope"),
                    "low_slope": geometry.get("low_slope")},
    )
