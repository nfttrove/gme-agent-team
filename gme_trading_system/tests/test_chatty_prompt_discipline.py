"""
Tests for run_commentary's prompt — pins the bypass-pattern discipline
that stops Chatty hallucinating range numbers.

Observed failure mode (2026-05-11): Chatty's prompt previously dumped the
output of market_state.get_market_fact() verbatim, which contained both
today's range and the 5-day range in adjacent lines. Gemma at temperature
0.7 confused them and produced narrative like:

    "Buyers reclaim $24.34, testing resistance near $25.43.
     Today's range $23.69-$24.34."

…where $23.69 / $25.43 were the 5-day low/high, not today's range.

These tests pin the prompt structure so the bypass discipline can't
silently regress.
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
def seeded_db(tmp_path, monkeypatch):
    """Temp DB with a few ticks so get_market_fact returns real numbers."""
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    # Today: range 24.10–24.40, 4 ticks
    conn.executescript(
        """
        INSERT INTO price_ticks (symbol, timestamp, close, volume) VALUES
            ('GME', datetime('now', '-30 minutes'), 24.40, 1000),
            ('GME', datetime('now', '-20 minutes'), 24.25, 1000),
            ('GME', datetime('now', '-10 minutes'), 24.10, 1000),
            ('GME', datetime('now', '-1 minute'),  24.20, 1000);
        -- Yesterday's close, for prev_close lookup
        INSERT INTO price_ticks (symbol, timestamp, close, volume) VALUES
            ('GME', datetime('now', '-1 day'), 24.00, 1000);
        -- Older 5-day-low data point (not 'today') — the trap that caused the bug
        INSERT INTO price_ticks (symbol, timestamp, close, volume) VALUES
            ('GME', datetime('now', '-3 days'), 23.50, 1000);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(orchestrator, "DB_PATH", str(db))
    return str(db)


@pytest.fixture
def captured_prompt(monkeypatch):
    """Capture the prompt passed to llm_generate instead of running it."""
    seen = {}

    def fake_generate(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        return "GME steady at $24.20, holding session lows."

    import llm_config
    monkeypatch.setattr(llm_config, "llm_generate", fake_generate)
    return seen


class TestChattyPromptDiscipline:

    def test_prompt_separates_today_range_from_five_day_range(
        self, seeded_db, captured_prompt,
    ):
        """
        Given a DB where the 5-day range is wider than today's range
        When run_commentary builds its prompt
        Then today's range is labelled separately and the 5-day range is
        NOT dumped alongside it (the source of the historical confusion).

        Why this matters: regressing to dumping the full get_market_fact
        prompt_line would re-introduce the 'cited 5-day range as today's
        range' bug that shipped to Telegram on 2026-05-11.
        """
        orchestrator.run_commentary()
        prompt = captured_prompt.get("prompt", "")

        # Today's range line is present with explicit label
        assert "today's range:" in prompt
        # 5-day range is NOT in the prompt — Gemma should only see today's range
        assert "5-day range" not in prompt
        assert "5d range" not in prompt

    def test_prompt_forbids_inventing_levels(self, seeded_db, captured_prompt):
        """
        Given the prompt is built
        When run_commentary runs
        Then the RULES section explicitly forbids inventing support/resistance.

        Why this matters: without this rule Gemma will pattern-match its
        training-data and write 'testing resistance near $X' where $X is
        plausible-sounding but not in the FACTS.
        """
        orchestrator.run_commentary()
        prompt = captured_prompt.get("prompt", "")

        assert "NEVER invent support/resistance" in prompt
        assert "Cite ONLY the prices above" in prompt

    def test_prompt_uses_low_temperature(self, seeded_db, captured_prompt):
        """
        Given Chatty's job is numeric narration, not creative writing
        When run_commentary calls the LLM
        Then temperature is low (≤ 0.4) — high temp was the root cause of
        the conflation in the original bug.
        """
        orchestrator.run_commentary()
        kwargs = captured_prompt.get("kwargs", {})
        assert kwargs.get("temperature", 1.0) <= 0.4

    def test_prompt_locks_direction(self, seeded_db, captured_prompt):
        """
        Given get_market_fact has classified direction as FALLING/RISING/SIDEWAYS
        When run_commentary builds the prompt
        Then the direction word appears in the FACTS block and the rules
        forbid contradicting it.
        """
        orchestrator.run_commentary()
        prompt = captured_prompt.get("prompt", "")

        # One of the three directions must be present (we don't pin which)
        assert any(d in prompt for d in ("RISING", "FALLING", "SIDEWAYS"))
        assert "Direction must match" in prompt

    def test_prompt_handles_missing_prev_close_gracefully(
        self, tmp_path, monkeypatch, captured_prompt,
    ):
        """
        Given a DB with no prior-day price (fresh deployment)
        When run_commentary builds the prompt
        Then the prev-close line says 'unavailable' rather than crashing
        on a None format.
        """
        # Given — DB with only today's ticks, no prior day
        db = tmp_path / "agent_memory.db"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO price_ticks (symbol, timestamp, close, volume) "
            "VALUES ('GME', datetime('now'), 24.20, 1000)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(orchestrator, "DB_PATH", str(db))

        # When
        orchestrator.run_commentary()

        # Then
        prompt = captured_prompt.get("prompt", "")
        assert "prev close" in prompt
        # No format crash, and graceful 'unavailable' rendering
        assert "unavailable" in prompt or "$24" in prompt  # one or the other
