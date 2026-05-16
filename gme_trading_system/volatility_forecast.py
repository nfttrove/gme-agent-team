"""Daily realized-volatility forecast for GME options context.

This is the production-safe version of the QuantConnect research note: use only
past daily GME closes to forecast next-day absolute log return. It deliberately
avoids a heavy ML dependency and validates on a chronological holdout so the
options job does not confuse an in-sample research R² with deployable edge.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Sequence

SYMBOL = "GME"
FEATURE_NAMES = ("lag1", "mean5", "mean10", "mean21", "diff1", "diff5", "diff21")


@dataclass(frozen=True)
class VolatilityForecast:
    ok: bool
    predicted_abs_return: float | None = None
    predicted_abs_move_pct: float | None = None
    validation_r2: float | None = None
    holdout_samples: int = 0
    train_samples: int = 0
    feature_names: tuple[str, ...] = FEATURE_NAMES
    method: str = "har_ridge_abs_return"
    reason: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"Realized-vol forecast unavailable: {self.reason}"
        r2 = "n/a" if self.validation_r2 is None else f"{self.validation_r2:.2f}"
        return (
            f"Realized-vol forecast: next-day |GME return| ≈ {self.predicted_abs_move_pct:.2f}% "
            f"(walk-forward holdout R² {r2}, holdout n={self.holdout_samples}, "
            f"method={self.method}; research context only, not an options execution signal)"
        )


def load_daily_closes(db_path: str, symbol: str = SYMBOL) -> list[tuple[str, float]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT date, close
             FROM daily_candles
            WHERE symbol = ? AND close IS NOT NULL AND close > 0
         ORDER BY date ASC""",
        (symbol,),
    ).fetchall()
    conn.close()
    return [(str(date), float(close)) for date, close in rows]


def absolute_log_returns(closes: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
    returns: list[tuple[str, float]] = []
    for (date, close), (_, prev_close) in zip(closes[1:], closes[:-1]):
        if close > 0 and prev_close > 0:
            returns.append((date, abs(math.log(close / prev_close))))
    return returns


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_supervised_rows(abs_returns: Sequence[tuple[str, float]]) -> tuple[list[list[float]], list[float]]:
    """Build feature rows at day t to predict absolute return at day t+1."""
    values = [ret for _, ret in abs_returns]
    x_rows: list[list[float]] = []
    y_rows: list[float] = []
    for i in range(21, len(values) - 1):
        lag1 = values[i]
        row = [
            lag1,
            _mean(values[i - 4 : i + 1]),
            _mean(values[i - 9 : i + 1]),
            _mean(values[i - 20 : i + 1]),
            values[i] - values[i - 1],
            values[i] - values[i - 5],
            values[i] - values[i - 21],
        ]
        x_rows.append(row)
        y_rows.append(values[i + 1])
    return x_rows, y_rows


def _transpose(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    return [list(col) for col in zip(*matrix)]


def _matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    b_t = _transpose(b)
    return [[sum(x * y for x, y in zip(row, col)) for col in b_t] for row in a]


def _matvec(a: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    return [sum(x * y for x, y in zip(row, v)) for row in a]


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    """Small Gaussian-elimination solver for ridge normal equations."""
    n = len(b)
    aug = [row[:] + [rhs] for row, rhs in zip(a, b)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [v / scale for v in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [v - factor * p for v, p in zip(aug[row], aug[col])]
    return [aug[i][-1] for i in range(n)]


def _ridge_fit(x_rows: Sequence[Sequence[float]], y_rows: Sequence[float], alpha: float = 0.05) -> list[float]:
    if not x_rows:
        raise ValueError("no training rows")
    x = [[1.0, *row] for row in x_rows]
    xt = _transpose(x)
    xtx = _matmul(xt, x)
    for i in range(1, len(xtx)):  # do not penalize intercept
        xtx[i][i] += alpha
    xty = _matvec(xt, y_rows)
    return _solve_linear(xtx, xty)


def _predict(coefs: Sequence[float], row: Sequence[float]) -> float:
    return max(0.0, coefs[0] + sum(c * x for c, x in zip(coefs[1:], row)))


def _r2(actual: Sequence[float], predicted: Sequence[float]) -> float:
    if len(actual) < 2:
        return 0.0
    mean_y = _mean(actual)
    ss_tot = sum((y - mean_y) ** 2 for y in actual)
    if ss_tot <= 0:
        return 0.0
    ss_res = sum((y - yhat) ** 2 for y, yhat in zip(actual, predicted))
    return 1 - ss_res / ss_tot


def _latest_feature_row(abs_returns: Sequence[tuple[str, float]]) -> list[float]:
    values = [ret for _, ret in abs_returns]
    i = len(values) - 1
    if i < 21:
        raise ValueError("need at least 22 return observations")
    return [
        values[i],
        _mean(values[i - 4 : i + 1]),
        _mean(values[i - 9 : i + 1]),
        _mean(values[i - 20 : i + 1]),
        values[i] - values[i - 1],
        values[i] - values[i - 5],
        values[i] - values[i - 21],
    ]


def forecast_next_abs_return_from_closes(
    closes: Sequence[tuple[str, float]],
    min_samples: int = 80,
    holdout_fraction: float = 0.20,
) -> VolatilityForecast:
    abs_returns = absolute_log_returns(closes)
    x_rows, y_rows = build_supervised_rows(abs_returns)
    if len(y_rows) < min_samples:
        return VolatilityForecast(ok=False, reason=f"need {min_samples} supervised rows, got {len(y_rows)}")

    holdout = max(20, int(len(y_rows) * holdout_fraction))
    holdout = min(holdout, len(y_rows) - 30)
    if holdout <= 0:
        return VolatilityForecast(ok=False, reason="not enough rows for chronological holdout")

    split = len(y_rows) - holdout
    try:
        train_coefs = _ridge_fit(x_rows[:split], y_rows[:split])
        holdout_pred = [_predict(train_coefs, row) for row in x_rows[split:]]
        validation_r2 = _r2(y_rows[split:], holdout_pred)
        full_coefs = _ridge_fit(x_rows, y_rows)
        forecast = _predict(full_coefs, _latest_feature_row(abs_returns))
    except ValueError as exc:
        return VolatilityForecast(ok=False, reason=str(exc))

    return VolatilityForecast(
        ok=True,
        predicted_abs_return=forecast,
        predicted_abs_move_pct=forecast * 100,
        validation_r2=validation_r2,
        holdout_samples=holdout,
        train_samples=split,
    )


def forecast_next_abs_return(db_path: str, symbol: str = SYMBOL) -> VolatilityForecast:
    try:
        closes = load_daily_closes(db_path, symbol=symbol)
    except sqlite3.Error as exc:
        return VolatilityForecast(ok=False, reason=f"database error: {exc}")
    return forecast_next_abs_return_from_closes(closes)
