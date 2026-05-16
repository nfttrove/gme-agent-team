import math

from volatility_forecast import forecast_next_abs_return_from_closes


def _synthetic_closes(n=180):
    price = 20.0
    closes = []
    vol = 0.02
    for i in range(n):
        # Smooth, persistent absolute returns so the HAR-style features have a
        # predictable signal without relying on random test data.
        vol = 0.006 + 0.82 * vol + 0.002 * math.sin(i / 7)
        direction = -1 if i % 2 else 1
        price *= math.exp(direction * max(vol, 0.001))
        closes.append((f"2025-01-{(i % 28) + 1:02d}-{i:03d}", price))
    return closes


def test_forecast_next_abs_return_from_closes_uses_chronological_holdout():
    forecast = forecast_next_abs_return_from_closes(_synthetic_closes(), min_samples=80)

    assert forecast.ok is True
    assert forecast.predicted_abs_move_pct is not None
    assert forecast.predicted_abs_move_pct > 0
    assert forecast.validation_r2 is not None
    assert forecast.holdout_samples >= 20
    assert "walk-forward holdout R²" in forecast.summary()
    assert "not an options execution signal" in forecast.summary()


def test_forecast_next_abs_return_from_closes_soft_fails_on_small_samples():
    forecast = forecast_next_abs_return_from_closes(_synthetic_closes(40), min_samples=80)

    assert forecast.ok is False
    assert "need 80 supervised rows" in forecast.reason
    assert forecast.summary().startswith("Realized-vol forecast unavailable")
