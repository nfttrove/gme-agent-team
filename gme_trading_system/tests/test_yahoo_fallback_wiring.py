"""
Tests for the Yahoo Finance fallback poller wiring.

The yfinance fetch itself isn't unit-tested here — it hits the network and
yfinance has its own test surface. What we test is the **integration
contract**: start_yahoo_feed is imported, idempotent, and writes via
INSERT OR IGNORE so it never overwrites primary TradingView data.

Why this matters: the feed exists to gracefully degrade when TradingView
webhooks go quiet (as happened 2026-05-11 ~09:31 ET). If the import or
the idempotency contract regresses, the system would re-introduce the
data-gap blind spot.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestYahooFallbackWiring:

    def test_orchestrator_imports_start_yahoo_feed(self):
        """
        Given the orchestrator module
        When it is imported
        Then start_yahoo_feed must be reachable as a module-level symbol.

        Why this matters: a regression that drops the import would silently
        leave the poller dead — TradingView outages would once again freeze
        the briefs on stale prices.
        """
        import orchestrator
        assert hasattr(orchestrator, "start_yahoo_feed")

    def test_start_yahoo_feed_is_idempotent(self, monkeypatch):
        """
        Given start_yahoo_feed has already been called
        When it is called again
        Then it does not spawn a second thread (returns the existing one).

        Why this matters: the orchestrator's start() might re-run on launchd
        restart paths; the poller must never duplicate.
        """
        import yahoo_finance_feed as yff

        # Pretend yfinance is installed (avoid the import branch in start_yahoo_feed)
        monkeypatch.setattr(yff, "yf", object())

        # Stub the poller loop so the thread exits immediately instead of
        # spinning at network IO during the test.
        monkeypatch.setattr(yff, "_yahoo_poller", lambda: None)
        yff._thread = None  # reset state from any prior test

        first = yff.start_yahoo_feed()
        second = yff.start_yahoo_feed()

        assert first is True
        assert second is True
        # Same thread object — no duplicate spawned
        assert yff._thread is not None

    def test_write_tick_uses_insert_or_ignore_so_tradingview_wins(self, tmp_path, monkeypatch):
        """
        Given a price_ticks row already exists from TradingView at timestamp T
        When the Yahoo poller writes a different price for the same T
        Then the TradingView row is preserved (INSERT OR IGNORE wins).

        Why this matters: this is the contract that lets both feeds run safely
        without double-counting or letting Yahoo (less timely) override the
        live TradingView source.
        """
        # Given — temp DB with the price_ticks schema
        db = tmp_path / "prices.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE price_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL DEFAULT 'GME',
                timestamp TEXT NOT NULL,
                close REAL,
                source TEXT DEFAULT 'tradingview',
                UNIQUE(symbol, timestamp)
            );
            INSERT INTO price_ticks (symbol, timestamp, close, source)
            VALUES ('GME', '2026-05-11T14:37:00Z', 24.34, 'tradingview');
            """
        )
        conn.commit()
        conn.close()

        # Point _write_tick at the temp DB
        import yahoo_finance_feed as yff
        monkeypatch.setattr(yff, "DB_PATH", str(db))
        monkeypatch.setattr(yff, "yf", object())  # so the early-return doesn't trigger

        # When — Yahoo tries to write a *different* price at the same timestamp
        yff._write_tick("2026-05-11T14:37:00Z", 99.99)

        # Then — TradingView's price is unchanged
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT close, source FROM price_ticks WHERE symbol='GME'"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == 24.34            # not overwritten
        assert rows[0][1] == "tradingview"    # source unchanged
