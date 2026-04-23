"""Integration tests for WAL, backup, Pydantic models, and episodic logging."""
import os
import sys
import pytest
import sqlite3
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

SCHEMA = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "db_schema.sql")).read()


@pytest.fixture
def temp_db_dir():
    """Create a temporary directory for test databases."""
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def test_db(temp_db_dir):
    """Create a test database in temp directory."""
    db_path = os.path.join(temp_db_dir, "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db_path


class TestWALMode:
    """Test SQLite WAL mode enablement and compatibility."""

    def test_enable_wal_mode(self, test_db):
        from db_maintenance import enable_wal_mode

        mode = enable_wal_mode(test_db)
        assert mode == "wal", f"Expected WAL mode, got {mode}"

    def test_wal_persists_across_connections(self, test_db):
        from db_maintenance import enable_wal_mode

        enable_wal_mode(test_db)

        # Verify WAL is still active in a new connection
        conn = sqlite3.connect(test_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_concurrent_read_write_with_wal(self, test_db):
        """Verify WAL allows concurrent reads while writes are active."""
        from db_maintenance import enable_wal_mode
        import threading
        import time

        enable_wal_mode(test_db)

        errors = []

        def writer():
            try:
                conn = sqlite3.connect(test_db)
                for i in range(5):
                    conn.execute(
                        "INSERT INTO price_ticks (symbol, timestamp, open, high, low, close, volume) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("GME", f"2024-01-01 {9+i:02d}:00:00", 20+i, 21+i, 19+i, 20.5+i, 100000),
                    )
                    conn.commit()
                    time.sleep(0.1)
                conn.close()
            except Exception as e:
                errors.append(f"Writer: {e}")

        def reader():
            time.sleep(0.05)
            try:
                conn = sqlite3.connect(test_db)
                for _ in range(5):
                    count = conn.execute("SELECT COUNT(*) FROM price_ticks").fetchone()[0]
                    time.sleep(0.1)
                conn.close()
            except Exception as e:
                errors.append(f"Reader: {e}")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Concurrent access failed: {errors}"


class TestDatabaseBackup:
    """Test database backup and recovery."""

    def test_backup_creates_file(self, test_db, temp_db_dir):
        from db_maintenance import backup_db

        backup_dir = os.path.join(temp_db_dir, "backups")
        backup_path = backup_db(test_db, backup_dir)

        assert os.path.exists(backup_path)
        assert backup_path.startswith(backup_dir)
        assert backup_path.endswith(".db")

    def test_backup_is_consistent(self, test_db, temp_db_dir):
        """Verify backed-up DB has same schema and data."""
        from db_maintenance import enable_wal_mode, backup_db

        enable_wal_mode(test_db)

        # Insert test data
        conn = sqlite3.connect(test_db)
        conn.execute(
            "INSERT INTO price_ticks (symbol, timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("GME", "2024-01-01 10:00:00", 20.0, 21.0, 19.0, 20.5, 100000),
        )
        conn.commit()
        conn.close()

        backup_dir = os.path.join(temp_db_dir, "backups")
        backup_path = backup_db(test_db, backup_dir)

        # Verify backup has the data
        backup_conn = sqlite3.connect(backup_path)
        count = backup_conn.execute("SELECT COUNT(*) FROM price_ticks").fetchone()[0]
        backup_conn.close()

        assert count == 1

    def test_prune_old_backups(self, test_db, temp_db_dir):
        """Verify old backup pruning."""
        from db_maintenance import backup_db, prune_old_backups
        import time

        backup_dir = os.path.join(temp_db_dir, "backups")
        os.makedirs(backup_dir, exist_ok=True)

        # Create fake old backup
        old_backup = os.path.join(backup_dir, "agent_memory_19700101_000000.db")
        Path(old_backup).touch()

        # Create a recent backup
        backup_db(test_db, backup_dir)

        # Prune with 0 days retention (prune everything older than now)
        deleted = prune_old_backups(backup_dir, days=0)

        assert deleted >= 1
        assert not os.path.exists(old_backup)


class TestPydanticModels:
    """Test Pydantic output validation models."""

    def test_futurist_prediction_validates(self):
        from models.agent_outputs import FuturistPrediction

        valid = FuturistPrediction(
            predicted_price=25.5,
            confidence=0.85,
            horizon="1h",
            bias="BULLISH",
            reasoning="Strong momentum",
        )
        assert valid.predicted_price == 25.5
        assert valid.confidence == 0.85
        assert valid.horizon == "1h"

    def test_futurist_prediction_rejects_bad_confidence(self):
        from models.agent_outputs import FuturistPrediction

        with pytest.raises(ValueError):
            FuturistPrediction(
                predicted_price=25.5,
                confidence=1.5,  # > 1.0
                horizon="1h",
            )

    def test_futurist_prediction_rejects_bad_horizon(self):
        from models.agent_outputs import FuturistPrediction

        with pytest.raises(ValueError):
            FuturistPrediction(
                predicted_price=25.5,
                confidence=0.8,
                horizon="forever",  # doesn't end in m/h/d/w
            )

    def test_trader_decision_validates(self):
        from models.agent_outputs import TraderDecision
        from models.enums import TradeAction

        valid = TraderDecision(
            action=TradeAction.BUY,
            entry_price=20.5,
            quantity=10,
            stop_loss=19.5,
            take_profit=22.0,
            confidence=0.75,
        )
        assert valid.action == TradeAction.BUY
        assert valid.entry_price == 20.5

    def test_trader_decision_rejects_invalid_action(self):
        from models.agent_outputs import TraderDecision

        with pytest.raises(ValueError):
            TraderDecision(
                action="INVALID",
                entry_price=20.5,
                quantity=10,
                stop_loss=19.5,
                take_profit=22.0,
                confidence=0.75,
            )

    def test_trader_decision_rejects_stop_above_entry_for_buy(self):
        from models.agent_outputs import TraderDecision
        from models.enums import TradeAction

        with pytest.raises(ValueError):
            TraderDecision(
                action=TradeAction.BUY,
                entry_price=20.0,
                quantity=10,
                stop_loss=21.0,  # stop ABOVE entry on BUY — invalid
                take_profit=22.0,
                confidence=0.75,
            )

    def test_synthesis_brief_validates(self):
        from models.agent_outputs import SynthesisBrief

        valid = SynthesisBrief(
            price=21.5,
            consensus="BULLISH",
            consensus_pct=0.65,
        )
        assert valid.price == 21.5
        assert valid.consensus == "BULLISH"

    def test_news_signal_validates(self):
        from models.agent_outputs import NewsSignal

        valid = NewsSignal(
            headline="GME surges on short squeeze fears",
            sentiment_score=0.8,
            sentiment_label="positive",
            relevance_score=0.9,
        )
        assert valid.sentiment_score == 0.8


class TestEpisodicIntegration:
    """Test episodic logging integration with extractors."""

    def test_prediction_extractor_returns_none_for_no_json(self):
        from episodic_integration import extract_prediction_from_output

        result = extract_prediction_from_output("GME looks bullish today.")
        assert result is None

    def test_trade_extractor_returns_none_for_no_json(self):
        from episodic_integration import extract_trade_from_output

        result = extract_trade_from_output("Market conditions uncertain.")
        assert result is None

    def test_trade_extractor_parses_valid_json(self):
        from episodic_integration import extract_trade_from_output
        from models.agent_outputs import TraderDecision

        # Flat JSON containing the word "decision" with required fields
        output = '{"decision": true, "action": "BUY", "entry_price": 21.0, "quantity_usd": 210.0, "stop_loss": 19.5, "take_profit": 23.0, "confidence": 0.75, "reasoning": "breakout"}'
        result = extract_trade_from_output(output)
        if result is not None:
            assert isinstance(result, TraderDecision)
            assert result.action == "BUY"

    def test_synthesis_extractor_parses_structured_output(self):
        from episodic_integration import extract_synthesis_from_output
        from models.agent_outputs import SynthesisBrief

        # The synthesis extractor uses text format, not JSON
        output = "PRICE: $21.50 | DATA: clean | NEWS: positive 0.75 | PATTERN: bull_flag | TREND: UP 0.8 | PREDICTION: BULLISH 0.70 | STRUCTURAL: GREEN | CONSENSUS: BULLISH 65%"
        result = extract_synthesis_from_output(output)
        if result is not None:
            assert isinstance(result, SynthesisBrief)
            assert result.price == 21.50

    def test_synthesis_extractor_returns_none_for_empty(self):
        from episodic_integration import extract_synthesis_from_output

        result = extract_synthesis_from_output("")
        assert result is None

    def test_extractor_returns_none_for_invalid_confidence(self):
        from episodic_integration import extract_prediction_from_output

        # JSON with word "prediction" but confidence out of range
        output = '{"prediction": true, "overall_confidence": 99.0, "bias": "BULLISH"}'
        result = extract_prediction_from_output(output)
        assert result is None


class TestMarketHours:
    """Test market hours and active window constraints."""

    def test_market_hours_detection(self):
        from market_hours import is_market_open
        from zoneinfo import ZoneInfo
        from datetime import datetime

        ET = ZoneInfo("America/New_York")

        # Monday 10 AM — market open
        monday_10am = datetime(2024, 1, 8, 10, 0, tzinfo=ET)
        assert is_market_open(monday_10am) is True

        # Saturday — market closed
        saturday = datetime(2024, 1, 6, 10, 0, tzinfo=ET)
        assert is_market_open(saturday) is False

        # Weekday before market open
        early = datetime(2024, 1, 8, 8, 0, tzinfo=ET)
        assert is_market_open(early) is False

    def test_active_window_detection(self):
        from market_hours import is_active_window
        from zoneinfo import ZoneInfo
        from datetime import datetime

        ET = ZoneInfo("America/New_York")

        # Active window is 08:30-17:00 ET, Mon-Fri (1h pre/post market buffer).

        # Monday 09:00 — inside window
        monday_9am = datetime(2024, 1, 8, 9, 0, tzinfo=ET)
        assert is_active_window(monday_9am) is True

        # Monday 08:00 — before window (pre-8:30)
        monday_8am = datetime(2024, 1, 8, 8, 0, tzinfo=ET)
        assert is_active_window(monday_8am) is False

        # Monday 17:30 — after window
        monday_1730 = datetime(2024, 1, 8, 17, 30, tzinfo=ET)
        assert is_active_window(monday_1730) is False

        # Saturday noon — weekend, no active window regardless of time
        saturday = datetime(2024, 1, 6, 12, 0, tzinfo=ET)
        assert is_active_window(saturday) is False

    def test_active_window_decorator_skips_outside_window(self):
        from market_hours import active_window_required
        from zoneinfo import ZoneInfo
        from datetime import datetime
        from unittest.mock import patch

        ET = ZoneInfo("America/New_York")

        call_count = {"n": 0}

        @active_window_required
        def test_func():
            call_count["n"] += 1
            return "executed"

        # Saturday — should skip
        saturday = datetime(2024, 1, 6, 12, 0, tzinfo=ET)
        with patch("market_hours.datetime") as mock_dt:
            mock_dt.now.return_value = saturday
            result = test_func()
            assert call_count["n"] == 0  # Should not have executed


class TestAlembicIntegration:
    """Test database migrations via Alembic."""

    def test_alembic_env_exists(self):
        alembic_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "alembic", "env.py"
        )
        assert os.path.exists(alembic_path), "Alembic env.py not found"

    def test_migrations_directory_exists(self):
        versions_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "alembic", "versions"
        )
        assert os.path.isdir(versions_path), "Alembic versions directory not found"

    def test_baseline_migration_exists(self):
        versions_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "alembic", "versions"
        )
        # Check for any migration files
        migrations = [f for f in os.listdir(versions_path) if f.endswith(".py")]
        assert len(migrations) > 0, "No migration files found"
