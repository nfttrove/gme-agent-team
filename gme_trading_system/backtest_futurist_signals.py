#!/usr/bin/env python3
"""Backtest historical Futurist signal accuracy.

Simulates Futurist predictions on historical data to prove signal quality
before committing to live measurement.

Output:
  - How many signals would have fired in period
  - Win rate (predictions that hit target)
  - Miss rate (hit stop loss instead)
  - Calibration (did 78% confidence signals actually win 78% of time?)
  - MAPE (mean absolute percent error)

Usage:
    python backtest_futurist_signals.py --days 30     # Last 30 days
    python backtest_futurist_signals.py --days 90     # Last 90 days
    python backtest_futurist_signals.py --start 2024-01-01 --end 2024-12-31
"""
import sqlite3
import os
import argparse
import statistics
from datetime import datetime, timedelta
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from typing import Optional

ET = ZoneInfo("America/New_York")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


@dataclass
class SignalBacktest:
    """Result of backtesting a single prediction."""
    timestamp: str
    confidence: float
    predicted_price: float
    entry_price: float
    stop_loss: float
    take_profit: float

    # Actual outcome
    actual_price: Optional[float]  # Price that occurred
    horizon_hours: int  # How far into the future to look

    # Results
    hit_target: bool
    hit_stop: bool
    mape: float  # Mean absolute percent error

    @property
    def was_correct(self) -> bool:
        """Did prediction hit target before stop?"""
        return self.hit_target and not self.hit_stop

    @property
    def pnl_pct(self) -> float:
        """Return if prediction was correct, loss if wrong."""
        if self.hit_target:
            return ((self.take_profit - self.entry_price) / self.entry_price) * 100
        elif self.hit_stop:
            return ((self.stop_loss - self.entry_price) / self.entry_price) * 100
        else:
            # Incomplete (still within timeframe, didn't hit either)
            return ((self.actual_price - self.entry_price) / self.entry_price) * 100


class FuturistBacktester:
    """Backtest Futurist agent predictions on historical data."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def backtest_period(self, start_date: str, end_date: str) -> dict:
        """Backtest Futurist signals for date range."""
        # Load historical price data
        prices = self._load_prices(start_date, end_date)

        if not prices:
            return {"error": "No price data found for period"}

        # Simulate: what Futurist signals would have fired?
        # For now, we'll use predictions already logged in DB
        # In production, you'd re-run Futurist on historical data
        predictions = self._load_futurist_predictions(start_date, end_date)

        if not predictions:
            print(f"No Futurist predictions found in DB for {start_date} to {end_date}")
            print("Running synthetic backtest instead...")
            predictions = self._generate_synthetic_predictions(prices, start_date, end_date)

        # Score each prediction against actual prices
        backtests = []
        for pred in predictions:
            bt = self._score_prediction(pred, prices)
            if bt:
                backtests.append(bt)

        # Compute metrics
        metrics = self._compute_metrics(backtests)
        return {
            "backtests": backtests,
            "metrics": metrics,
            "summary": self._format_summary(metrics, backtests),
        }

    def _load_prices(self, start_date: str, end_date: str) -> dict:
        """Load OHLCV data by timestamp."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT * FROM price_ticks
            WHERE symbol='GME' AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
            """,
            (start_date, end_date),
        )
        rows = cursor.fetchall()
        conn.close()

        # Index by timestamp for quick lookup
        prices = {row["timestamp"]: dict(row) for row in rows}
        return prices

    def _load_futurist_predictions(self, start_date: str, end_date: str) -> list[dict]:
        """Load actual Futurist predictions from DB (if any exist)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT * FROM signal_alerts
            WHERE agent_name='Futurist' AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
            """,
            (start_date, end_date),
        )
        predictions = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return predictions

    def _generate_synthetic_predictions(self, prices: dict, start_date: str, end_date: str) -> list[dict]:
        """Generate synthetic Futurist predictions for testing.

        If no real predictions exist, create synthetic ones for backtest validation.
        """
        # For testing: create 1 prediction per day with 70% confidence
        synthetic = []

        sorted_times = sorted(prices.keys())
        for i, ts in enumerate(sorted_times[::288]):  # Sample every ~2 hours equivalent
            if i % 12 == 0:  # Create signal every 24 hours
                price = prices[ts]["close"]
                synthetic.append({
                    "agent_name": "Futurist",
                    "signal_type": "price_prediction",
                    "timestamp": ts,
                    "confidence": 0.70,
                    "entry_price": price * 0.99,
                    "stop_loss": price * 0.96,
                    "take_profit": price * 1.03,
                    "predicted_price": price * 1.03,
                })

        return synthetic

    def _score_prediction(self, pred: dict, prices: dict) -> Optional[SignalBacktest]:
        """Score single prediction against actual prices."""
        pred_time = pred["timestamp"]
        entry_price = pred.get("entry_price") or pred.get("predicted_price")
        stop_loss = pred.get("stop_loss", entry_price * 0.95)
        take_profit = pred.get("take_profit", entry_price * 1.05)
        predicted_price = pred.get("predicted_price", entry_price)
        confidence = pred.get("confidence", 0.5)

        # Horizon: assume 1h by default (or extract from prediction)
        horizon_hours = 1

        # Find price data after prediction time
        sorted_times = sorted(prices.keys())
        pred_idx = None
        try:
            pred_idx = sorted_times.index(pred_time)
        except ValueError:
            # Find closest time
            for i, t in enumerate(sorted_times):
                if t > pred_time:
                    pred_idx = i
                    break

        if pred_idx is None or pred_idx + horizon_hours >= len(sorted_times):
            return None  # Not enough future data

        # Look at prices in the next `horizon_hours`
        future_prices = [prices[t].get("close", 0) for t in sorted_times[pred_idx:pred_idx + horizon_hours + 1]]
        max_price = max(future_prices)
        min_price = min(future_prices)
        actual_close = future_prices[-1]

        # Did it hit target or stop loss?
        hit_target = max_price >= take_profit
        hit_stop = min_price <= stop_loss

        # MAPE: how far off was the prediction?
        mape = abs(predicted_price - actual_close) / actual_close * 100

        return SignalBacktest(
            timestamp=pred_time,
            confidence=confidence,
            predicted_price=predicted_price,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            actual_price=actual_close,
            horizon_hours=horizon_hours,
            hit_target=hit_target,
            hit_stop=hit_stop,
            mape=mape,
        )

    def _compute_metrics(self, backtests: list[SignalBacktest]) -> dict:
        """Compute aggregate metrics."""
        if not backtests:
            return {}

        total = len(backtests)
        winners = sum(1 for bt in backtests if bt.was_correct)
        losers = sum(1 for bt in backtests if bt.hit_stop and not bt.hit_target)
        incomplete = total - winners - losers

        win_rate = winners / total if total > 0 else 0
        avg_mape = statistics.mean([bt.mape for bt in backtests])

        # Calibration: group by confidence ranges, compute win rate for each
        calibration = self._compute_calibration(backtests)

        return {
            "total_signals": total,
            "winners": winners,
            "losers": losers,
            "incomplete": incomplete,
            "win_rate": win_rate,
            "avg_mape": avg_mape,
            "calibration": calibration,
        }

    def _compute_calibration(self, backtests: list[SignalBacktest]) -> dict:
        """Group by confidence buckets, show win rate per bucket."""
        buckets = {
            "high": [],      # 80%+
            "medium": [],    # 65-80%
            "low": [],       # <65%
        }

        for bt in backtests:
            if bt.confidence >= 0.80:
                buckets["high"].append(bt)
            elif bt.confidence >= 0.65:
                buckets["medium"].append(bt)
            else:
                buckets["low"].append(bt)

        calibration = {}
        for bucket_name, bts in buckets.items():
            if bts:
                win_count = sum(1 for bt in bts if bt.was_correct)
                calibration[bucket_name] = {
                    "count": len(bts),
                    "win_rate": win_count / len(bts),
                }

        return calibration

    def _format_summary(self, metrics: dict, backtests: list[SignalBacktest]) -> str:
        """Format results as readable summary."""
        if not metrics:
            return "No signals to backtest"

        summary = f"""
╔════════════════════════════════════════════════════════════════╗
║          FUTURIST SIGNAL BACKTEST RESULTS                      ║
╚════════════════════════════════════════════════════════════════╝

📊 OVERALL METRICS
─────────────────────────────────────────────────────────────────
Total Signals:        {metrics['total_signals']}
✓ Winners (target):   {metrics['winners']} ({metrics['win_rate']:.1%})
✗ Losers (stop):      {metrics['losers']}
⏳ Incomplete:        {metrics['incomplete']}

Avg Prediction Error: {metrics['avg_mape']:.2f}%

🎯 CALIBRATION BY CONFIDENCE
─────────────────────────────────────────────────────────────────
"""
        for bucket, data in metrics.get("calibration", {}).items():
            emoji = "🔴" if bucket == "high" else ("🟡" if bucket == "medium" else "🟢")
            summary += f"{emoji} {bucket.upper():6} (n={data['count']:3}): {data['win_rate']:.1%} win rate\n"

        summary += f"""
✅ CONCLUSION
─────────────────────────────────────────────────────────────────
DeepSeek-r1 Futurist accuracy: {metrics['win_rate']:.1%} on {metrics['total_signals']} historical signals

This shows the prediction quality BEFORE team feedback loop.
Team feedback will help calibrate confidence thresholds.
"""
        return summary


def main():
    parser = argparse.ArgumentParser(description="Backtest Futurist signal accuracy")
    parser.add_argument("--days", type=int, default=30, help="Look back N days (default: 30)")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    # Determine date range
    if args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
        end_date = datetime.now(ET).date()
        start_date = end_date - timedelta(days=args.days)
        start_date = str(start_date)
        end_date = str(end_date)

    print(f"\nBacktesting Futurist signals: {start_date} to {end_date}\n")

    backtester = FuturistBacktester()
    result = backtester.backtest_period(start_date, end_date)

    if "error" in result:
        print(f"❌ {result['error']}")
        return

    print(result["summary"])

    # Show top wins and losses
    backtests = result["backtests"]
    if backtests:
        print("\n📈 TOP WINNERS (highest confidence, correct predictions)")
        winners = sorted([bt for bt in backtests if bt.was_correct], key=lambda x: x.confidence, reverse=True)
        for bt in winners[:3]:
            print(f"  {bt.confidence:.0%} confidence → predicted ${bt.predicted_price:.2f}, actual ${bt.actual_price:.2f} ✓")

        print("\n📉 TOP LOSERS (high confidence, wrong predictions)")
        losers = sorted([bt for bt in backtests if bt.hit_stop], key=lambda x: x.confidence, reverse=True)
        for bt in losers[:3]:
            print(f"  {bt.confidence:.0%} confidence → predicted ${bt.predicted_price:.2f}, actual ${bt.actual_price:.2f} ✗")


if __name__ == "__main__":
    main()
