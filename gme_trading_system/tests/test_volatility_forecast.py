import math

from volatility_forecast import forecast_next_abs_return_from_closes


def _synthetic_closes(n: int = 120, vol: float = 0.02) -> list[float]:
    """Persistent-vol walk so the rolling-mean baseline has a stable signal."""
    price = 20.0
    out = [price]
    for i in range(n - 1):
        vol = 0.006 + 0.82 * vol + 0.002 * math.sin(i / 7)
        direction = -1 if i % 2 else 1
        price *= math.exp(direction * max(vol, 0.001))
        out.append(price)
    return out


def test_baseline_predicts_positive_next_day_move_and_includes_regime():
    forecast = forecast_next_abs_return_from_closes(_synthetic_closes(120))

    assert forecast.ok is True
    assert forecast.predicted_abs_move_pct > 0
    assert forecast.sample_size == 21
    assert forecast.long_term_abs_move_pct is not None
    summary = forecast.summary()
    assert "21d rolling mean" in summary
    assert "not an options execution signal" in summary
    assert "90d" in summary


def test_baseline_omits_regime_when_fewer_than_long_window_samples():
    forecast = forecast_next_abs_return_from_closes(_synthetic_closes(40))

    assert forecast.ok is True
    assert forecast.long_term_abs_move_pct is None
    assert "90d" not in forecast.summary()


def test_baseline_soft_fails_on_too_few_closes():
    forecast = forecast_next_abs_return_from_closes([20.0, 20.5, 21.0])

    assert forecast.ok is False
    assert "need 22 closes" in forecast.reason
    assert forecast.summary().startswith("Realized-vol baseline unavailable")
