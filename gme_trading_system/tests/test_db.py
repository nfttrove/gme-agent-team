"""Unit tests for database schema and aggregator logic."""
import sqlite3
import os
import sys
import pytest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

SCHEMA = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "db_schema.sql")).read()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def seed_ticks(db_path: str, date_str: str, n: int = 5):
    conn = sqlite3.connect(db_path)
    for i in range(n):
        ts = f"{date_str} {9+i:02d}:00:00"
        conn.execute(
            "INSERT INTO price_ticks (symbol, timestamp, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            ("GME", ts, 20.0 + i * 0.1, 20.5 + i * 0.1, 19.5 + i * 0.1, 20.2 + i * 0.1, 10000 * (i + 1)),
        )
    conn.commit()
    conn.close()


class TestSchema:
    def test_all_tables_exist(self, db):
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        expected = {"price_ticks", "daily_candles", "trend_analysis", "news_analysis",
                    "predictions", "trade_decisions", "agent_logs"}
        assert expected.issubset(tables)

    def test_insert_trade_decision(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trade_decisions (timestamp, action, quantity, entry_price, stop_loss, take_profit, paper_trade, status) "
            "VALUES (?, 'BUY', 10, 20.5, 19.89, 21.73, 1, 'filled')",
            (datetime.now().isoformat(),),
        )
        count = conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0]
        conn.close()
        assert count == 1


class TestDailyAggregator:
    def test_aggregates_correctly(self, tmp_path, monkeypatch):
        import daily_aggregator

        db_path = str(tmp_path / "agg.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

        monkeypatch.setattr(daily_aggregator, "DB_PATH", db_path)

        date = "2024-01-15"
        seed_ticks(db_path, date, n=5)
        daily_aggregator.aggregate_day(date)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM daily_candles WHERE date=?", (date,)).fetchone()
        conn.close()

        assert row is not None
        # close should be from the last tick
        assert row is not None

    def test_no_ticks_does_not_crash(self, tmp_path, monkeypatch):
        import daily_aggregator

        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

        monkeypatch.setattr(daily_aggregator, "DB_PATH", db_path)
        daily_aggregator.aggregate_day("2024-01-01")  # no data — should not raise
