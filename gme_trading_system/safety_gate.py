"""
Pre-LLM safety gate — inspired by jackson-video-resources/claude-tradingview-mcp-trading.

Evaluates all strategy conditions deterministically BEFORE calling any agent.
If no condition set passes, the pipeline is aborted entirely — saving LLM cost
and preventing the agents from manufacturing a rationale for a bad trade.

Usage:
    from safety_gate import SafetyGate
    gate = SafetyGate()
    result = gate.evaluate(candles)
    if not result.allowed:
        print(result.report())
        sys.exit(0)
    print(f"Bias: {result.bias}, signal: {result.signal}")
"""
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, date

from indicators import compute_all, Candle

STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "strategy.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


@dataclass
class ConditionResult:
    label: str
    passed: bool
    actual: float | bool | str
    expected: float | bool | str


@dataclass
class GateResult:
    allowed: bool
    signal: str                         # "long" | "short" | "none"
    bias: str                           # "bullish" | "bearish" | "neutral"
    indicators: dict = field(default_factory=dict)
    long_checks: list[ConditionResult] = field(default_factory=list)
    short_checks: list[ConditionResult] = field(default_factory=list)
    blocker: str = ""

    def report(self) -> str:
        lines = [
            f"=== Safety Gate ({'PASS' if self.allowed else 'BLOCK'}) ===",
            f"Signal: {self.signal}  |  Bias: {self.bias}",
            f"Price={self.indicators.get('price')}  VWAP={self.indicators.get('vwap')}  "
            f"EMA8={self.indicators.get('ema8')}  RSI3={self.indicators.get('rsi3')}  "
            f"RSI14={self.indicators.get('rsi14')}  ATR14={self.indicators.get('atr14')}",
            "",
        ]
        if self.long_checks:
            lines.append("LONG conditions:")
            for c in self.long_checks:
                icon = "✅" if c.passed else "❌"
                lines.append(f"  {icon} {c.label}  (got: {c.actual})")
        if self.short_checks:
            lines.append("SHORT conditions:")
            for c in self.short_checks:
                icon = "✅" if c.passed else "❌"
                lines.append(f"  {icon} {c.label}  (got: {c.actual})")
        if self.blocker:
            lines.append(f"\nBlocked: {self.blocker}")
        return "\n".join(lines)


def _eval_condition(indicator_val, op: str, target) -> bool:
    ops = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
        ">":  lambda a, b: a > b,
        "<":  lambda a, b: a < b,
    }
    fn = ops.get(op)
    return bool(fn(indicator_val, target)) if fn else False


def _daily_trade_count() -> int:
    """Fail-hard: if DB is unreachable, block all trading rather than allow unlimited trades."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    today = date.today().isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM trade_decisions WHERE timestamp LIKE ? AND status != 'rejected'",
        (f"{today}%",),
    ).fetchone()[0]
    conn.close()
    return count


class SafetyGate:
    def __init__(self, strategy_path: str = STRATEGY_PATH):
        with open(strategy_path) as f:
            self.strategy = json.load(f)

    def evaluate(self, candles: list[Candle]) -> GateResult:
        ind = compute_all(candles)
        if not ind:
            return GateResult(allowed=False, signal="none", bias="neutral",
                              blocker="No candle data available")

        # Daily trade limit — fail-hard if DB unreachable
        daily_limit = self.strategy["risk"]["daily_trade_limit"]
        try:
            trades_today = _daily_trade_count()
        except Exception as e:
            return GateResult(allowed=False, signal="none", bias="neutral",
                              indicators=ind,
                              blocker=f"DB unreachable — trading suspended: {e}")
        if trades_today >= daily_limit:
            return GateResult(allowed=False, signal="none", bias="neutral",
                              indicators=ind,
                              blocker=f"Daily trade limit reached ({trades_today}/{daily_limit})")

        # Note: live IBKR daily-loss check happens at execution time (broker.execute_trade_decision),
        # not here — IBKR connectivity issues should not block analysis agents.

        # Determine market bias
        bias_rules = self.strategy["bias_detection"]
        bias = "neutral"
        for b, conditions in bias_rules.items():
            if all(ind.get(k) == v for k, v in conditions.items()):
                bias = b
                break

        # Evaluate long conditions
        long_checks = self._check_conditions(
            self.strategy["long_entry"]["conditions"], ind
        )
        short_checks = self._check_conditions(
            self.strategy["short_entry"]["conditions"], ind
        )

        long_passed  = all(c.passed for c in long_checks)
        short_passed = all(c.passed for c in short_checks)

        if long_passed and bias == "bullish":
            return GateResult(
                allowed=True, signal="long", bias=bias, indicators=ind,
                long_checks=long_checks, short_checks=short_checks,
            )
        if short_passed and bias == "bearish":
            return GateResult(
                allowed=True, signal="short", bias=bias, indicators=ind,
                long_checks=long_checks, short_checks=short_checks,
            )

        failed_long  = [c.label for c in long_checks  if not c.passed]
        failed_short = [c.label for c in short_checks if not c.passed]
        blocker = f"Long failed: {failed_long} | Short failed: {failed_short}"

        return GateResult(
            allowed=False, signal="none", bias=bias, indicators=ind,
            long_checks=long_checks, short_checks=short_checks,
            blocker=blocker,
        )

    def _check_conditions(self, conditions: list[dict], ind: dict) -> list[ConditionResult]:
        results = []
        for cond in conditions:
            key    = cond["indicator"]
            op     = cond["operator"]
            target = cond["value"]
            actual = ind.get(key)
            passed = _eval_condition(actual, op, target) if actual is not None else False
            results.append(ConditionResult(
                label=cond["label"], passed=passed, actual=actual, expected=target
            ))
        return results


def run_gate_check(lookback_days: int = 10) -> GateResult:
    """Convenience: fetch latest candles and run the gate. Returns GateResult."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from tools import PriceDataTool

    raw = PriceDataTool()._run(lookback_days=lookback_days)
    candles: list[Candle] = [
        {
            "open":   float(r.get("open", 0)),
            "high":   float(r.get("high", 0)),
            "low":    float(r.get("low", 0)),
            "close":  float(r.get("close", 0)),
            "volume": float(r.get("volume", 0)),
        }
        for r in raw if r.get("close")
    ]
    gate = SafetyGate()
    return gate.evaluate(candles)


if __name__ == "__main__":
    result = run_gate_check()
    print(result.report())
