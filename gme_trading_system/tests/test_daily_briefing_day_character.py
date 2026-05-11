"""
Tests for the day-of-week character + £5k tracker in run_daily_briefing.

Style: behaviour-focused names + G/W/T docstrings. Each test names the
real-world scenario it protects (Monday's gap context, Friday's opex
context, first-Friday NFP morning, deadline tracker rendering).
"""
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import orchestrator  # noqa: E402


SCHEMA = open(os.path.join(REPO_ROOT, "db_schema.sql")).read()
SIGNAL_ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_alerts (
    id          TEXT PRIMARY KEY,
    agent_name  TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    confidence  REAL NOT NULL,
    severity    TEXT,
    entry_price REAL,
    stop_loss   REAL,
    take_profit REAL,
    reasoning   TEXT,
    telegram_message_id INTEGER,
    timestamp   TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);
"""


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executescript(SIGNAL_ALERTS_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(orchestrator, "DB_PATH", str(db))
    return str(db)


@pytest.fixture
def captured_telegram(monkeypatch):
    sent = []
    import notifier
    monkeypatch.setattr(notifier, "notify", lambda text, **kw: sent.append(text))
    return sent


@pytest.fixture
def stub_llm(monkeypatch):
    import llm_config
    monkeypatch.setattr(
        llm_config, "llm_generate",
        lambda *a, **kw: "PATTERN: stub\nWAITING_FOR: stub\nRISK: stub",
    )


@pytest.fixture
def freeze_today(monkeypatch):
    """Freeze the `date.today()` used inside run_daily_briefing.

    Returns a setter so each test pins its own weekday.
    """
    class _DateProxy:
        _frozen = date(2026, 5, 11)  # Monday

        @classmethod
        def today(cls):
            return cls._frozen

    # The function imports `date` locally, so we patch via the orchestrator module's
    # injection of date.today() through the module-level import.
    import orchestrator as orch
    orch_date_cls = type("_FrozenDateModule", (), {"today": staticmethod(lambda: _DateProxy._frozen)})
    # Easier path: patch orchestrator._day_intro to compute against a frozen date
    # by replacing date inside run_daily_briefing's local namespace. We do that
    # by monkeypatching the `date` symbol the orchestrator module would resolve
    # via the inner import.
    import datetime as dt_module

    class _FrozenDate(dt_module.date):
        @classmethod
        def today(cls):
            return _DateProxy._frozen

    # Patch datetime.date globally for the test — narrow scope, restored on teardown
    monkeypatch.setattr(dt_module, "date", _FrozenDate)

    def _set(d: date):
        _DateProxy._frozen = d

    return _set


def _seed_minimal_price_facts(db_path):
    """Insert the minimum daily_candles + price_ticks rows so run_daily_briefing
    doesn't bail on 'No price data'."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        INSERT INTO daily_candles (symbol, date, open, high, low, close, volume)
        VALUES ('GME', date('now'),         25.00, 25.50, 24.80, 25.20, 1000000),
               ('GME', date('now', '-1 day'), 24.50, 25.00, 24.20, 24.80,  900000);

        INSERT INTO price_ticks (symbol, close, volume, timestamp)
        VALUES ('GME', 25.20, 5000, datetime('now'));
        """
    )
    conn.commit()
    conn.close()


# ── Pure _day_intro tests ─────────────────────────────────────────────────────

class TestDayIntro:

    def test_monday_intro_mentions_weekend_gap_risk(self):
        """
        Given a Monday date
        When _day_intro is called
        Then the fact line mentions gap-risk and the tag says 'first day'.
        """
        tag, line = orchestrator._day_intro(date(2026, 5, 11))  # Monday
        assert "first day" in tag.lower()
        assert "gap-risk" in line or "weekend" in line

    def test_friday_intro_mentions_opex(self):
        """
        Given a regular (non-first) Friday
        When _day_intro is called
        Then the line mentions weekly opex and the tag says 'opex day'.

        Why this matters: weekly options expiry is the single biggest Friday
        flow event. The brief must surface it as context for Gemma.
        """
        tag, line = orchestrator._day_intro(date(2026, 5, 22))  # 3rd Friday of May (not 1st)
        assert "opex" in tag.lower()
        assert "options expire" in line.lower() or "opex" in line.lower()
        assert "NFP" not in line  # not the first Friday

    def test_first_friday_intro_appends_nfp_note(self):
        """
        Given the first Friday of a month (day <= 7)
        When _day_intro is called
        Then the fact line additionally mentions NFP at 08:30.

        Why this matters: Non-Farm Payrolls drops at 08:30 ET on the first
        Friday of the month and routinely moves the open. Brief omitting it
        would be operationally negligent.
        """
        tag, line = orchestrator._day_intro(date(2026, 5, 1))  # first Friday of May
        assert "opex" in tag.lower()
        assert "NFP" in line
        assert "08:30" in line

    def test_wednesday_intro_mentions_mid_week_pulse(self):
        tag, line = orchestrator._day_intro(date(2026, 5, 13))  # Wednesday
        assert "pulse" in tag.lower()
        assert "mid-week" in line.lower() or "thesis" in line.lower()

    def test_tuesday_thursday_have_distinct_tags(self):
        tue_tag, _ = orchestrator._day_intro(date(2026, 5, 12))  # Tuesday
        thu_tag, _ = orchestrator._day_intro(date(2026, 5, 14))  # Thursday
        assert "confirmation" in tue_tag.lower()
        assert "pre-opex" in thu_tag.lower()
        assert tue_tag != thu_tag


# ── Integration: full run_daily_briefing ──────────────────────────────────────

class TestDailyBriefingWithDayCharacter:

    def test_monday_brief_header_carries_first_day_tag(
        self, empty_db, captured_telegram, stub_llm, freeze_today,
    ):
        """
        Given today is a Monday
        When run_daily_briefing fires
        Then the Telegram header includes the 'Monday — first day' tag.

        Why this matters: the team sees the tag in the chat and immediately
        knows the brief's lens (gap-risk / weekend context).
        """
        # Given
        freeze_today(date(2026, 5, 11))  # Monday
        _seed_minimal_price_facts(empty_db)

        # When
        orchestrator.run_daily_briefing()

        # Then
        assert len(captured_telegram) == 1
        msg = captured_telegram[0]
        assert "DAILY STRATEGY BRIEF — Monday — first day" in msg

    def test_friday_brief_header_carries_opex_tag(
        self, empty_db, captured_telegram, stub_llm, freeze_today,
    ):
        """
        Given today is a Friday
        When run_daily_briefing fires
        Then the header includes 'Friday — opex day' and the team sees the lens.
        """
        # Given
        freeze_today(date(2026, 5, 22))  # Friday
        _seed_minimal_price_facts(empty_db)

        # When
        orchestrator.run_daily_briefing()

        # Then
        msg = captured_telegram[0]
        assert "Friday — opex day" in msg

    def test_brief_does_not_leak_private_5k_target(
        self, empty_db, captured_telegram, stub_llm, freeze_today,
    ):
        """
        Given the daily brief is the team-facing broadcast
        When run_daily_briefing fires
        Then the brief MUST NOT contain the personal £5k target — that lives
        in /progress, an owner-only Telegram command.

        Why this matters: the £5k figure is a private monthly goal. The
        broadcast must stay focused on the trade. If a future refactor
        re-adds the line, this test fails immediately.
        """
        # Given
        freeze_today(date(2026, 5, 11))
        _seed_minimal_price_facts(empty_db)

        # When
        orchestrator.run_daily_briefing()

        # Then
        msg = captured_telegram[0]
        assert "£5K" not in msg
        assert "5K BY 2026-05-31" not in msg
        assert "deadline" not in msg.lower()

    def test_first_friday_brief_prompt_includes_nfp_context(
        self, empty_db, captured_telegram, freeze_today, monkeypatch,
    ):
        """
        Given today is the first Friday of the month
        When run_daily_briefing builds Gemma's prompt
        Then the FACTS block includes the NFP note.

        Why this matters: Gemma's narrative needs the NFP context to write
        a coherent RISK section that morning. Captured via prompt sniffing.
        """
        # Given
        freeze_today(date(2026, 5, 1))  # first Friday
        _seed_minimal_price_facts(empty_db)
        captured_prompts = []
        import llm_config
        def capture_prompt(prompt, **kw):
            captured_prompts.append(prompt)
            return "PATTERN: stub\nWAITING_FOR: stub\nRISK: stub"
        monkeypatch.setattr(llm_config, "llm_generate", capture_prompt)

        # When
        orchestrator.run_daily_briefing()

        # Then
        assert any("NFP" in p for p in captured_prompts), \
            f"Expected NFP in prompt; got {len(captured_prompts)} prompts"
