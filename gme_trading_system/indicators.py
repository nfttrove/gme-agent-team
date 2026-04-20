"""
Deterministic indicator calculations — no LLM, no hallucination.
All functions take a list of OHLCV dicts (sorted oldest-first) and return floats.
"""
from typing import TypedDict


class Candle(TypedDict):
    open: float
    high: float
    low: float
    close: float
    volume: float


def ema(closes: list[float], period: int) -> float:
    """Exponential Moving Average — returns the latest value."""
    if len(closes) < period:
        return sum(closes) / len(closes)
    k = 2 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1 - k)
    return val


def sma(closes: list[float], period: int) -> float:
    data = closes[-period:]
    return sum(data) / len(data)


def rsi(closes: list[float], period: int = 14) -> float:
    """Wilder RSI — returns latest value (0–100)."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def vwap(candles: list[Candle]) -> float:
    """Session VWAP — typical price * volume / cumulative volume."""
    total_tp_vol = 0.0
    total_vol = 0.0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        total_tp_vol += tp * c["volume"]
        total_vol += c["volume"]
    return total_tp_vol / total_vol if total_vol else 0.0


def atr(candles: list[Candle], period: int = 14) -> float:
    """Average True Range."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return 0.0
    atr_val = sum(trs[:period]) / min(period, len(trs))
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def compute_all(candles: list[Candle]) -> dict:
    """
    Compute the full indicator snapshot from a list of candles.
    Returns a dict suitable for passing to the safety gate and agents.
    """
    if not candles:
        return {}

    closes = [float(c["close"]) for c in candles]
    latest = candles[-1]
    price = float(latest["close"])

    vwap_val = vwap(candles)
    ema8 = ema(closes, 8)
    ema21 = ema(closes, 21)
    ema50 = ema(closes, 50)
    rsi3 = rsi(closes, 3)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)

    pct_from_vwap = ((price - vwap_val) / vwap_val * 100) if vwap_val else 0

    return {
        "price": round(price, 4),
        "vwap": round(vwap_val, 4),
        "ema8": round(ema8, 4),
        "ema21": round(ema21, 4),
        "ema50": round(ema50, 4),
        "rsi3": round(rsi3, 2),
        "rsi14": round(rsi14, 2),
        "atr14": round(atr14, 4),
        "pct_from_vwap": round(pct_from_vwap, 3),
        "above_vwap": price > vwap_val,
        "above_ema8": price > ema8,
        "above_ema21": price > ema21,
        "above_ema50": price > ema50,
        "volume": float(latest.get("volume", 0)),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
    }
