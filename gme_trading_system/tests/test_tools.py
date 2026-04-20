"""Unit tests for tools.py — no LLM calls, no network required."""
import sqlite3
import os
import sys
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Create a fresh in-memory-style DB with the schema applied."""
    db_file = str(tmp_path / "test.db")
    schema = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "db_schema.sql")).read()
    conn = sqlite3.connect(db_file)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO price_ticks (symbol, timestamp, open, high, low, close, volume) "
        "VALUES ('GME', '2024-01-15 10:00:00', 20.0, 21.0, 19.5, 20.5, 100000)"
    )
    conn.execute(
        "INSERT INTO daily_candles (symbol, date, open, high, low, close, volume, vwap) "
        "VALUES ('GME', '2024-01-15', 20.0, 21.0, 19.5, 20.5, 500000, 20.3)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", db_file)
    import tools
    monkeypatch.setattr(tools, "DB_PATH", db_file)
    return db_file


class TestSQLQueryTool:
    def test_select_returns_rows(self, tmp_db):
        from tools import SQLQueryTool
        tool = SQLQueryTool()
        rows = tool._run("SELECT * FROM price_ticks WHERE symbol='GME'")
        assert len(rows) == 1
        assert rows[0]["close"] == pytest.approx(20.5)

    def test_rejects_non_select(self, tmp_db):
        from tools import SQLQueryTool
        tool = SQLQueryTool()
        result = tool._run("DROP TABLE price_ticks")
        assert result[0].get("error") is not None

    def test_handles_bad_sql(self, tmp_db):
        from tools import SQLQueryTool
        tool = SQLQueryTool()
        result = tool._run("SELECT * FROM nonexistent_table")
        assert result[0].get("error") is not None


class TestPriceDataTool:
    def test_returns_candles_from_db(self, tmp_db):
        from tools import PriceDataTool
        tool = PriceDataTool()
        rows = tool._run(lookback_days=10)
        assert len(rows) >= 1
        assert rows[0]["symbol"] == "GME"

    def test_yfinance_fallback_on_empty_db(self, tmp_path, monkeypatch):
        """When DB has no candles, falls back to yfinance (mocked)."""
        import tools

        empty_db = str(tmp_path / "empty.db")
        conn = sqlite3.connect(empty_db)
        conn.execute(
            "CREATE TABLE daily_candles (id INTEGER PRIMARY KEY, symbol TEXT, date TEXT, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL, vwap REAL, created_at TEXT)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(tools, "DB_PATH", empty_db)

        fake_rows = [{"date": "2024-01-15", "open": 20.0, "high": 21.0,
                      "low": 19.5, "close": 20.5, "volume": 500000}]

        tool = tools.PriceDataTool()
        monkeypatch.setattr(tool, "_yfinance_fallback", lambda d: fake_rows)
        result = tool._run(lookback_days=5)
        assert result == fake_rows


class TestNewsAPITool:
    def test_returns_list(self, monkeypatch):
        from tools import NewsAPITool
        import tools

        monkeypatch.delenv("NEWSAPI_KEY", raising=False)
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)

        tool = NewsAPITool()
        result = tool._finnhub_news()
        assert isinstance(result, list)
        assert len(result) >= 1
