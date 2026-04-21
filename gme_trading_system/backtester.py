"""Historical backtester for GME trading system.

Replays past trade_decisions against actual price data to compute:
- Win rate and total P&L
- Sharpe ratio (risk-adjusted returns)
- Max drawdown
- Prediction accuracy by horizon

Usage:
    python backtester.py --start 2024-01-01 --end 2024-12-31
    python backtester.py --last-days 90
"""
import sqlite3
import os
import argparse
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from zoneinfo import ZoneInfo
import statistics

ET = ZoneInfo("America/New_York")

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


@dataclass
class TradeMetrics:
    """Aggregated trade metrics."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    sharpe_ratio: float
    max_drawdown: float


@dataclass
class PredictionMetrics:
    """Prediction accuracy metrics."""
    total_predictions: int
    hit_rate_5pct: float
    mape: float


class Backtester:
    """Historical backtester for GME trading system."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def run(self, start_date: str, end_date: str) -> dict:
        """Run backtest for given date range. Returns metrics dict."""
        trades = self._load_trades(start_date, end_date)
        candles = self._load_candles(start_date, end_date)
        predictions = self._load_predictions(start_date, end_date)

        trade_metrics = self._compute_trade_metrics(trades, candles)
        pred_metrics = self._compute_prediction_metrics(predictions)

        return {
            "trades": trade_metrics,
            "predictions": pred_metrics,
            "summary": self._format_summary(trade_metrics, pred_metrics),
        }

    def _load_trades(self, start_date: str, end_date: str) -> list[dict]:
        """Load paper trades within date range."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM trade_decisions
            WHERE paper_trade=1 AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp
            """,
            (start_date, end_date + " 23:59:59"),
        )
        trades = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return trades

    def _load_candles(self, start_date: str, end_date: str) -> list[dict]:
        """Load daily candles within date range."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM daily_candles
            WHERE symbol='GME' AND date >= ? AND date <= ?
            ORDER BY date
            """,
            (start_date, end_date),
        )
        candles = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return candles

    def _load_predictions(self, start_date: str, end_date: str) -> list[dict]:
        """Load predictions within date range."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM predictions
            WHERE actual_price IS NOT NULL AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp
            """,
            (start_date, end_date + " 23:59:59"),
        )
        predictions = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return predictions

    def _compute_trade_metrics(self, trades: list[dict], candles: list[dict]) -> TradeMetrics:
        """Compute trade performance metrics."""
        if not trades:
            return TradeMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)

        # Build candle lookup for exit price
        candle_by_date = {c["date"]: c for c in candles}

        pnl_list = []
        winning = 0
        losing = 0

        for trade in trades:
            if trade["exit_price"] is None:
                continue

            pnl = trade.get("pnl")
            if pnl is None:
                continue

            pnl_list.append(pnl)
            if pnl > 0:
                winning += 1
            elif pnl < 0:
                losing += 1

        total = winning + losing
        if total == 0:
            return TradeMetrics(len(trades), 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)

        total_pnl = sum(pnl_list)
        avg_pnl = total_pnl / total if total > 0 else 0.0
        win_rate = winning / total if total > 0 else 0.0

        # Sharpe ratio: (mean return) / (std dev) * sqrt(252) — assumes daily returns
        sharpe = 0.0
        if len(pnl_list) > 1:
            try:
                std_dev = statistics.stdev(pnl_list)
                if std_dev > 0:
                    sharpe = (statistics.mean(pnl_list) / std_dev) * (252 ** 0.5)
            except:
                pass

        # Max drawdown
        max_drawdown = self._compute_max_drawdown(pnl_list)

        return TradeMetrics(
            total_trades=len(trades),
            winning_trades=winning,
            losing_trades=losing,
            win_rate=win_rate,
            total_pnl=total_pnl,
            avg_pnl=avg_pnl,
            sharpe_ratio=sharpe,
            max_drawdown=max_drawdown,
        )

    def _compute_prediction_metrics(self, predictions: list[dict]) -> PredictionMetrics:
        """Compute prediction accuracy metrics."""
        if not predictions:
            return PredictionMetrics(0, 0.0, 0.0)

        hit_5pct = 0
        errors = []

        for pred in predictions:
            predicted = pred.get("predicted_price")
            actual = pred.get("actual_price")

            if predicted is None or actual is None or predicted <= 0 or actual <= 0:
                continue

            # Within ±5%
            lower_bound = predicted * 0.95
            upper_bound = predicted * 1.05
            if lower_bound <= actual <= upper_bound:
                hit_5pct += 1

            # MAPE
            error_pct = abs(actual - predicted) / predicted * 100
            errors.append(error_pct)

        mape = statistics.mean(errors) if errors else 0.0
        hit_rate = hit_5pct / len(predictions) if predictions else 0.0

        return PredictionMetrics(
            total_predictions=len(predictions),
            hit_rate_5pct=hit_rate,
            mape=mape,
        )

    def _compute_max_drawdown(self, pnl_list: list[float]) -> float:
        """Compute maximum drawdown from peak to trough."""
        if not pnl_list:
            return 0.0

        cumulative = []
        running_sum = 0
        for pnl in pnl_list:
            running_sum += pnl
            cumulative.append(running_sum)

        if not cumulative:
            return 0.0

        peak = cumulative[0]
        max_dd = 0.0
        for value in cumulative:
            if value > peak:
                peak = value
            dd = (peak - value) / peak if peak != 0 else 0
            max_dd = max(max_dd, dd)

        return max_dd

    def _format_summary(self, trades: TradeMetrics, preds: PredictionMetrics) -> str:
        """Format results as readable summary."""
        lines = [
            "",
            "╔════════════════════════════════════════════════════════╗",
            "║              BACKTEST RESULTS                          ║",
            "╚════════════════════════════════════════════════════════╝",
            "",
            f"  Total Trades:        {trades.total_trades}",
            f"  Winning:             {trades.winning_trades} ({trades.win_rate*100:.1f}%)",
            f"  Losing:              {trades.losing_trades}",
            f"  Total P&L:           ${trades.total_pnl:,.2f}",
            f"  Avg P&L/Trade:       ${trades.avg_pnl:,.2f}",
            f"  Sharpe Ratio:        {trades.sharpe_ratio:.2f}",
            f"  Max Drawdown:        {trades.max_drawdown*100:.1f}%",
            "",
            f"  Predictions:         {preds.total_predictions}",
            f"  Hit Rate (±5%):      {preds.hit_rate_5pct*100:.1f}%",
            f"  MAPE:                {preds.mape:.2f}%",
            "",
        ]
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Backtest GME trading system")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", help="Start date (YYYY-MM-DD)")
    group.add_argument("--last-days", type=int, help="Last N days")

    parser.add_argument("--end", help="End date (YYYY-MM-DD), defaults to today")

    args = parser.parse_args()

    end = args.end or date.today().isoformat()

    if args.last_days:
        end_date = date.fromisoformat(end)
        start_date = end_date - timedelta(days=args.last_days)
        start = start_date.isoformat()
    else:
        start = args.start

    print(f"Backtesting {start} to {end}...")

    backtester = Backtester()
    results = backtester.run(start, end)

    print(results["summary"])


if __name__ == "__main__":
    main()
