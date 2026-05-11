"""
Tests for run_monday_weekend_digest() — the Monday 08:00 ET pre-open brief.

Style: behaviour-focused names + G/W/T docstrings. The 'why this matters' line
ties each test to a real Monday-morning scenario: weekend headline storm,
no news, missing pre-market quote, GeoRisk weekend event, Gemma offline.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import orchestrator  # noqa: E402


SCHEMA = open(os.path.join(REPO_ROOT, "db_schema.sql")).read()


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """Schema-only DB. Pins orchestrator.DB_PATH to it."""
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(orchestrator, "DB_PATH", str(db))
    return str(db)


@pytest.fixture
def captured_telegram(monkeypatch):
    """Capture notify() calls instead of hitting Telegram."""
    sent = []
    import notifier
    monkeypatch.setattr(notifier, "notify", lambda text, **kw: sent.append(text))
    return sent


@pytest.fixture
def stub_llm(monkeypatch):
    """Stub Gemma so tests don't need Ollama running."""
    import llm_config
    monkeypatch.setattr(
        llm_config, "llm_generate",
        lambda *a, **kw: "Watch the first 30-min range; fade extremes.",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMondayWeekendDigest:

    def test_when_no_weekend_news_then_brief_still_sends_with_zero_count(
        self, empty_db, captured_telegram, stub_llm,
    ):
        """
        Given a DB with no news_analysis rows since Friday
        When run_monday_weekend_digest fires
        Then a Telegram brief still sends, reporting '0 items' and 'No news flagged'.

        Why this matters: quiet weekends are common. The brief must not skip
        sending or crash — silence on Monday morning is itself information.
        """
        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        assert len(captured_telegram) == 1
        msg = captured_telegram[0]
        assert "MONDAY WEEKEND DIGEST" in msg
        assert "0 items" in msg
        assert "No news flagged since Friday close" in msg

    def test_when_weekend_news_exists_then_top_5_headlines_appear(
        self, empty_db, captured_telegram, stub_llm,
    ):
        """
        Given 7 news rows since Friday with labelled sentiments
        When run_monday_weekend_digest fires
        Then the brief lists the 5 most recent headlines with sentiment labels.

        Why this matters: weekend news drives the Monday open. Truncating to
        5 keeps the message readable; ordering by recency means the freshest
        catalysts surface first.
        """
        # Given
        conn = sqlite3.connect(empty_db)
        for i in range(7):
            conn.execute(
                "INSERT INTO news_analysis (timestamp, headline, source, "
                "sentiment_label, sentiment_score) "
                "VALUES (datetime('now', ?), ?, 'finnhub', ?, ?)",
                (f"-{i} hours", f"GME headline {i}", "bullish" if i % 2 else "bearish", 0.7),
            )
        conn.commit()
        conn.close()

        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        msg = captured_telegram[0]
        assert "7 items" in msg
        # Five most recent (i=0..4) appear; i=5 and i=6 do not
        for i in range(5):
            assert f"GME headline {i}" in msg
        assert "GME headline 5" not in msg
        assert "GME headline 6" not in msg

    def test_when_premarket_quote_and_friday_close_exist_then_gap_is_computed(
        self, empty_db, captured_telegram, stub_llm,
    ):
        """
        Given Friday's daily candle close of $25 and a pre-market tick at $26
        When run_monday_weekend_digest fires
        Then the GAP SETUP line shows '+4.0% — gap up'.

        Why this matters: gap direction is the single most actionable
        Monday-morning data point. Wrong direction = bad first trade.
        """
        # Given
        conn = sqlite3.connect(empty_db)
        conn.execute(
            "INSERT INTO daily_candles (symbol, date, open, high, low, close, volume) "
            "VALUES ('GME', date('now', '-3 days'), 25.0, 25.5, 24.5, 25.0, 1000000)"
        )
        conn.execute(
            "INSERT INTO price_ticks (symbol, close, volume, timestamp) "
            "VALUES ('GME', 26.0, 5000, datetime('now', '-1 hours'))"
        )
        conn.commit()
        conn.close()

        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        msg = captured_telegram[0]
        assert "$26.00" in msg
        assert "$25.00" in msg
        assert "+4.0%" in msg
        assert "gap up" in msg

    def test_when_premarket_quote_missing_then_gap_section_says_unavailable(
        self, empty_db, captured_telegram, stub_llm,
    ):
        """
        Given no price_ticks at all (TradingView webhook hasn't fired)
        When run_monday_weekend_digest fires
        Then the GAP SETUP line says 'pre-market quote unavailable', not crash.

        Why this matters: TradingView webhook gaps happen. The brief degrading
        to 'unavailable' is honest; an invented gap number is worse than none.
        """
        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        msg = captured_telegram[0]
        assert "pre-market quote unavailable" in msg

    def test_when_georisk_log_exists_then_georisk_section_appears(
        self, empty_db, captured_telegram, stub_llm,
    ):
        """
        Given a GeoRisk agent_logs entry from the weekend
        When run_monday_weekend_digest fires
        Then the brief includes a GEORISK section with the log content.
        """
        # Given
        conn = sqlite3.connect(empty_db)
        conn.execute(
            "INSERT INTO agent_logs (agent_name, task_type, content, status, timestamp) "
            "VALUES ('GeoRisk', 'georisk', "
            "'Tensions escalated in region X over weekend; oil supply risk', "
            "'ok', datetime('now', '-12 hours'))"
        )
        conn.commit()
        conn.close()

        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        msg = captured_telegram[0]
        assert "GEORISK" in msg
        assert "Tensions escalated" in msg

    def test_when_no_georisk_then_georisk_section_is_omitted(
        self, empty_db, captured_telegram, stub_llm,
    ):
        """
        Given no GeoRisk logs since Friday
        When run_monday_weekend_digest fires
        Then the brief omits the GEORISK section entirely (not 'GEORISK: none').

        Why this matters: empty sections are noise. The bypass-pattern
        discipline is 'show what's there, don't fabricate scaffolding'.
        """
        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        msg = captured_telegram[0]
        assert "GEORISK" not in msg

    def test_when_llm_fails_then_fallback_watch_line_still_ships(
        self, empty_db, captured_telegram, monkeypatch,
    ):
        """
        Given Gemma is unreachable (e.g. Ollama down)
        When run_monday_weekend_digest fires
        Then the brief still sends with the hardcoded fallback 'WATCH' line.

        Same discipline as run_daily_briefing — facts ship even when narrative
        falls over.
        """
        # Given
        import llm_config
        def boom(*a, **kw):
            raise RuntimeError("Ollama down")
        monkeypatch.setattr(llm_config, "llm_generate", boom)

        # When
        orchestrator.run_monday_weekend_digest()

        # Then
        assert len(captured_telegram) == 1
        msg = captured_telegram[0]
        assert "WATCH" in msg
        assert "first 30-min range" in msg  # the fallback text
