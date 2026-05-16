"""Realized-volatility baseline for GME options context.

21-day rolling mean of |daily log return|, with a 90-day regime comparison.
Honest baseline because daily GME |return| is dominated by news/social/regulatory
shocks (squeezes, RC moves, FTD spikes) that a close-only model cannot see — a
heavier ML stack tested at R²≈0.04 on holdout, basically tied with this baseline.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

SYMBOL = "GME"
RECENT_WINDOW = 21
LONG_WINDOW = 90


@dataclass(frozen=True)
class VolatilityForecast:
    ok: bool
    predicted_abs_move_pct: float | None = None
    long_term_abs_move_pct: float | None = None
    sample_size: int = 0
    reason: str = ""

    @property
    def regime(self) -> str:
        """One-word regime label vs the 90d baseline, or empty string."""
        if not self.ok or not self.long_term_abs_move_pct or self.long_term_abs_move_pct <= 0:
            return ""
        ratio = self.predicted_abs_move_pct / self.long_term_abs_move_pct
        if ratio >= 1.25:
            return "elevated"
        if ratio <= 0.80:
            return "subdued"
        return "in line"

    def summary(self) -> str:
        if not self.ok:
            return f"Realized-vol baseline unavailable: {self.reason}"
        regime_txt = ""
        if self.regime:
            connector = "vs" if self.regime != "in line" else "with"
            regime_txt = f" ({self.regime} {connector} 90d {self.long_term_abs_move_pct:.2f}%)"
        return (
            f"Realized-vol baseline: next-day |GME return| ≈ {self.predicted_abs_move_pct:.2f}% "
            f"({RECENT_WINDOW}d rolling mean){regime_txt}; context only, not an options execution signal"
        )


def _load_closes(db_path: str, symbol: str = SYMBOL) -> list[float]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT close FROM daily_candles WHERE symbol=? AND close IS NOT NULL AND close>0 ORDER BY date ASC",
            (symbol,),
        ).fetchall()
    return [float(c) for (c,) in rows]


def _abs_log_returns(closes: list[float]) -> list[float]:
    return [abs(math.log(b / a)) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]


def forecast_next_abs_return_from_closes(closes: list[float]) -> VolatilityForecast:
    if len(closes) < RECENT_WINDOW + 1:
        return VolatilityForecast(ok=False, reason=f"need {RECENT_WINDOW + 1} closes, got {len(closes)}")
    returns = _abs_log_returns(closes)
    if len(returns) < RECENT_WINDOW:
        return VolatilityForecast(ok=False, reason=f"need {RECENT_WINDOW} returns, got {len(returns)}")
    recent = returns[-RECENT_WINDOW:]
    predicted = sum(recent) / len(recent) * 100
    long_term = None
    if len(returns) >= LONG_WINDOW:
        long_term = sum(returns[-LONG_WINDOW:]) / LONG_WINDOW * 100
    return VolatilityForecast(
        ok=True,
        predicted_abs_move_pct=predicted,
        long_term_abs_move_pct=long_term,
        sample_size=len(recent),
    )


def forecast_next_abs_return(db_path: str, symbol: str = SYMBOL) -> VolatilityForecast:
    try:
        closes = _load_closes(db_path, symbol=symbol)
    except sqlite3.Error as exc:
        return VolatilityForecast(ok=False, reason=f"database error: {exc}")
    return forecast_next_abs_return_from_closes(closes)
