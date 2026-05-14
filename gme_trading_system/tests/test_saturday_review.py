"""
Tests for run_saturday_review() — the weekly digest pushed to Telegram on
Saturday morning.

Style: behaviour-focused names + Given/When/Then docstrings. The 'why this
matters' line in each docstring names the real-world failure mode the test
protects against (an empty DB on a quiet week, a stale agent, a stuck
circuit breaker — every one of these has shipped as a bug somewhere).
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import orchestrator  # noqa: E402


SCHEMA = open(os.path.join(REPO_ROOT, "db_schema.sql")).read()

# signal_alerts is defined in an alembic migration, not db_schema.sql.
# Hard-coded here to keep the test self-contained (a fresh test DB can't run alembic).
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
    """A schema-only DB — no rows. Pins orchestrator.DB_PATH to it."""
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
    """Capture every notifier.notify() call instead of hitting Telegram."""
    sent = []
    import notifier
    monkeypatch.setattr(notifier, "notify", lambda text, **kw: sent.append(text))
    return sent


@pytest.fixture
def stub_llm(monkeypatch):
    """Stub the Gemma narrative call so tests don't need Ollama running."""
    import llm_config
    monkeypatch.setattr(
        llm_config, "llm_generate",
        lambda *a, **kw: "Watch Pattern + Futurist confluence on the open.",
    )


@pytest.fixture
def stub_candidates(monkeypatch):
    """Stub the lesson-candidate reader so tests don't depend on jsonl files."""
    import lesson_producer
    monkeypatch.setattr(lesson_producer, "list_staged_candidates", lambda *a, **kw: [])


@pytest.fixture
def reset_breakers(monkeypatch):
    """Reset the circuit-breaker registry so prior tests' state doesn't leak."""
    import circuit_breaker
    monkeypatch.setattr(circuit_breaker, "_breakers", {})


def _seed_week_of_activity(db_path):
    """Helper: insert a representative week of trades, predictions, signals."""
    conn = sqlite3.connect(db_path)
    # 3 paper trades, 2 wins, 1 loss — $40 net PnL
    conn.executescript(
        """
        INSERT INTO trade_decisions (order_id, action, symbol, entry_price, exit_price,
                                     pnl, status, paper_trade, timestamp)
        VALUES
            ('o1', 'buy',  'GME', 25.00, 26.50, 30.00, 'closed', 1, datetime('now', '-2 days')),
            ('o2', 'buy',  'GME', 26.00, 26.20,  4.00, 'closed', 1, datetime('now', '-3 days')),
            ('o3', 'sell', 'GME', 27.00, 26.00, 20.00, 'closed', 1, datetime('now', '-4 days'));

        INSERT INTO predictions (horizon, predicted_price, actual_price,
                                 error_pct, confidence, timestamp)
        VALUES
            ('1h', 26.00, 26.30, 1.15, 0.7, datetime('now', '-1 day')),
            ('4h', 27.00, 26.50, 1.85, 0.6, datetime('now', '-2 days'));

        INSERT INTO signal_alerts (id, agent_name, signal_type, confidence,
                                   timestamp)
        VALUES
            ('s1', 'Pattern',  'pattern_signal',  0.80, datetime('now', '-1 day')),
            ('s2', 'Pattern',  'pattern_signal',  0.82, datetime('now', '-2 days')),
            ('s3', 'Futurist', 'price_prediction', 0.65, datetime('now', '-3 days'));

        INSERT INTO agent_logs (agent_name, task_type, content, status, timestamp)
        VALUES
            ('Pattern', 'pattern_analysis', 'ok', 'ok', datetime('now', '-1 hour')),
            ('Futurist','prediction',       'ok', 'ok', datetime('now', '-2 hours'));
        """
    )
    conn.commit()
    conn.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSaturdayReview:

    def test_when_db_is_empty_then_review_still_sends_with_zero_metrics(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given a fresh DB with no signals or predictions
        When run_saturday_review fires
        Then a Telegram message is still sent, gracefully reporting zeros.

        Why this matters: the first Saturday after deployment WILL run against
        a near-empty DB. The brief must degrade to 'no signals' rather than
        crash on division-by-zero or stop the scheduler.
        """
        # Given / When
        orchestrator.run_saturday_review()

        # Then
        assert len(captured_telegram) == 1
        msg = captured_telegram[0]
        assert "SATURDAY REVIEW" in msg
        assert "No signals emitted this week" in msg
        assert "No predictions scored this week" in msg

    def test_brief_does_not_leak_paper_trade_open_close_stats(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given a week with 3 closed paper trades in the DB
        When run_saturday_review fires
        Then paper-trade open/close counts, win rate, and PnL MUST NOT
        appear in the broadcast — they're private operator data.

        Why this matters: the bot is signals-only. Paper-trade outcomes
        belong in the owner-only /standup view, never in the broadcast
        brief. A regression that re-adds the line is exactly what this
        test catches.
        """
        # Given — three closed paper trades exist in the DB
        _seed_week_of_activity(empty_db)

        # When
        orchestrator.run_saturday_review()

        # Then — none of the trade-outcome strings should appear
        msg = captured_telegram[0]
        assert "paper trades" not in msg.lower()
        assert "win rate" not in msg.lower()
        assert "100%" not in msg
        assert "$+54" not in msg and "+$54" not in msg
        assert "W/" not in msg  # the W/L bucket format from the old line

    def test_brief_does_not_leak_private_5k_target(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given the Saturday review is the team-facing weekly digest
        When run_saturday_review fires
        Then the brief MUST NOT contain the personal £5k target — that lives
        in /progress, an owner-only command.

        Why this matters: the £5k figure is a private monthly goal. Including
        it in the broadcast leaks personal context the team doesn't need. A
        future contributor restoring it 'because the tests still pass' would
        regress the privacy boundary — this test names the invariant.
        """
        orchestrator.run_saturday_review()
        msg = captured_telegram[0]
        assert "£5K" not in msg
        assert "5K BY 2026-05-31" not in msg
        assert "deadline" not in msg.lower()

    def test_top_agent_is_named_when_signals_exist(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given Pattern emitted 2 signals and Futurist emitted 1 this week
        When run_saturday_review fires
        Then the brief names Pattern as the top signal generator.
        """
        # Given
        _seed_week_of_activity(empty_db)

        # When
        orchestrator.run_saturday_review()

        # Then
        msg = captured_telegram[0]
        assert "Top signal generator: Pattern" in msg
        assert "2 signals" in msg

    def test_when_circuit_breaker_is_open_then_system_line_calls_it_out(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given the supabase circuit breaker is in OPEN state
        When run_saturday_review fires
        Then the SYSTEM section names the open breaker, not 'all closed'.

        Why this matters: a stuck breaker means an agent is silently failing
        to push to its destination. The Saturday brief is when an operator
        actually reads system health — surfacing it here is what gets it fixed.
        """
        # Given
        from circuit_breaker import get_breaker, State
        b = get_breaker("supabase")
        b._state = State.OPEN  # direct state poke for the test

        # When
        orchestrator.run_saturday_review()

        # Then
        msg = captured_telegram[0]
        assert "Open breakers" in msg
        assert "supabase" in msg
        assert "All circuit breakers closed" not in msg

    def test_when_no_breakers_open_then_system_line_says_all_closed(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given no circuit breakers have failed
        When run_saturday_review fires
        Then the SYSTEM section says 'All circuit breakers closed.'
        """
        # When
        orchestrator.run_saturday_review()

        # Then
        msg = captured_telegram[0]
        assert "All circuit breakers closed" in msg

    def test_lesson_candidates_count_appears_in_brief(
        self, empty_db, captured_telegram, stub_llm, reset_breakers, monkeypatch,
    ):
        """
        Given 4 lesson candidates are staged for review
        When run_saturday_review fires
        Then the LESSONS section shows '4 candidates pending review'.

        Why this matters: the Saturday brief is the only batched reminder
        to triage the /candidates queue. Drift in this count is the visible
        symptom of the learning loop stalling.
        """
        # Given
        import lesson_producer
        monkeypatch.setattr(
            lesson_producer, "list_staged_candidates",
            lambda *a, **kw: [{"pattern_id": f"p{i}"} for i in range(4)],
        )

        # When
        orchestrator.run_saturday_review()

        # Then
        msg = captured_telegram[0]
        assert "4 candidates pending review" in msg

    def test_dv_section_appears_when_snapshots_exist(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given the dv_score_history table has at least one snapshot
        When run_saturday_review fires
        Then a 🔍 DEEP VALUE section appears in the brief with each
        ticker's score, stars, and price.

        Why this matters: this is the operator-requested weekly DV digest.
        If the section silently stops appearing because dv_history's schema
        drifted or the cron stopped writing, this test catches it.
        """
        # Given
        from dv_history import SCHEMA as DV_SCHEMA
        conn = sqlite3.connect(empty_db)
        conn.executescript(DV_SCHEMA)
        conn.executescript(
            """
            INSERT INTO dv_score_history
                (ticker, score_date, score, rating, pillar_a, pillar_b,
                 pillar_c, pillar_d, price_at_score)
            VALUES
                ('CART', date('now'), 69.5, '★★★★☆', 20, 18, 16, 15, 40.85),
                ('GME',  date('now'), 65.4, '★★★★☆', 20, 17, 14, 14, 23.88);
            """
        )
        conn.commit()
        conn.close()

        # When
        orchestrator.run_saturday_review()

        # Then
        msg = captured_telegram[0]
        assert "DEEP VALUE" in msg
        assert "CART" in msg
        assert "GME" in msg
        assert "69.5" in msg
        assert "$40.85" in msg
        assert "★★★★☆" in msg

    def test_dv_section_shows_week_over_week_deltas_when_prior_snapshot_exists(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given two DV snapshots: today and 7 days ago, with score changes
        When run_saturday_review fires
        Then each ticker's line includes a delta tag like '↑ +1.5 vs last week'
        or '↓ -2.0 vs last week'.

        Why this matters: the week-over-week delta is the whole point of
        running this weekly — operators need to spot rank shifts, not stare
        at a static current snapshot.
        """
        from dv_history import SCHEMA as DV_SCHEMA
        conn = sqlite3.connect(empty_db)
        conn.executescript(DV_SCHEMA)
        conn.executescript(
            """
            INSERT INTO dv_score_history
                (ticker, score_date, score, rating, pillar_a, pillar_b,
                 pillar_c, pillar_d, price_at_score)
            VALUES
                -- 7 days ago: CART=68.0, GME=66.4
                ('CART', date('now','-7 days'), 68.0, '★★★★☆', 20, 17, 16, 15, 40.0),
                ('GME',  date('now','-7 days'), 66.4, '★★★★☆', 20, 18, 14, 14, 24.5),
                -- today: CART rose 1.5, GME fell 1.0
                ('CART', date('now'), 69.5, '★★★★☆', 20, 18, 16, 15, 40.85),
                ('GME',  date('now'), 65.4, '★★★★☆', 20, 17, 14, 14, 23.88);
            """
        )
        conn.commit()
        conn.close()

        orchestrator.run_saturday_review()
        msg = captured_telegram[0]

        # CART rose → up arrow + positive delta
        assert "↑ +1.5 vs last week" in msg
        # GME fell → down arrow + negative delta
        assert "↓ -1.0 vs last week" in msg
        # Neither line should still say 'first weekly snapshot' once we have history
        assert "first weekly snapshot" not in msg

    def test_dv_section_marks_new_entries(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given a prior snapshot exists for some tickers but a new ticker joined
        the watchlist between then and today
        When run_saturday_review fires
        Then the new ticker's line is tagged '(new entry)' rather than
        falsely showing a delta of 0.0.
        """
        from dv_history import SCHEMA as DV_SCHEMA
        conn = sqlite3.connect(empty_db)
        conn.executescript(DV_SCHEMA)
        conn.executescript(
            """
            INSERT INTO dv_score_history
                (ticker, score_date, score, rating, pillar_a, pillar_b,
                 pillar_c, pillar_d, price_at_score)
            VALUES
                ('GME',  date('now','-7 days'), 66.0, '★★★★☆', 20, 18, 14, 14, 24.5),
                ('GME',  date('now'),           65.4, '★★★★☆', 20, 17, 14, 14, 23.88),
                ('ALGN', date('now'),           65.2, '★★★★☆', 20, 16, 15, 14, 165.95);
            """
        )
        conn.commit()
        conn.close()

        orchestrator.run_saturday_review()
        msg = captured_telegram[0]
        assert "ALGN" in msg
        assert "(new entry)" in msg

    def test_dv_section_shows_all_tickers_not_just_top_n(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given a snapshot contains many more tickers than the old 8-row cap
        When run_saturday_review fires
        Then every ticker in the snapshot appears in the brief — including
        low-scoring ones — not just the top 8.

        Why this matters: operator requested visibility on the full watchlist
        (where the bottom is, not just who passed the deep-value gate).
        A regression to a row cap would silently hide most of the list.
        """
        from dv_history import SCHEMA as DV_SCHEMA
        conn = sqlite3.connect(empty_db)
        conn.executescript(DV_SCHEMA)
        # Seed 12 tickers with descending scores — past the old top_n=8 cap
        rows = [
            (f"T{i:02d}", f"date('now')", 80 - i * 5, "★★★★☆" if i < 3 else "★★☆☆☆")
            for i in range(12)
        ]
        for ticker, _, score, rating in rows:
            conn.execute(
                "INSERT INTO dv_score_history "
                "(ticker, score_date, score, rating, pillar_a, pillar_b, "
                " pillar_c, pillar_d, price_at_score) "
                "VALUES (?, date('now'), ?, ?, 20, 17, 14, 14, 25.0)",
                (ticker, score, rating),
            )
        conn.commit()
        conn.close()

        orchestrator.run_saturday_review()
        msg = captured_telegram[0]

        # Every single one should appear, including the 12th (lowest-score)
        for i in range(12):
            assert f"T{i:02d}" in msg, f"T{i:02d} missing from brief"

    def test_dv_section_omitted_when_history_empty(
        self, empty_db, captured_telegram, stub_llm, stub_candidates, reset_breakers,
    ):
        """
        Given dv_score_history has no rows yet (fresh deployment)
        When run_saturday_review fires
        Then the DV section is omitted entirely (not 'DV: no data').

        Why this matters: empty scaffolding is noise. The bypass-pattern
        discipline is 'show what's there, don't fabricate sections'.
        """
        # Given — table exists but is empty
        from dv_history import SCHEMA as DV_SCHEMA
        conn = sqlite3.connect(empty_db)
        conn.executescript(DV_SCHEMA)
        conn.close()

        # When
        orchestrator.run_saturday_review()

        # Then
        msg = captured_telegram[0]
        assert "DEEP VALUE" not in msg
        # The two surrounding sections must still appear so we know the brief
        # didn't fall over while skipping DV.
        assert "THIS WEEK" in msg
        assert "LESSONS" in msg

    def test_when_llm_narrative_fails_then_fallback_focus_line_appears(
        self, empty_db, captured_telegram, stub_candidates, reset_breakers, monkeypatch,
    ):
        """
        Given the LLM narrative call raises an exception (Ollama down)
        When run_saturday_review fires
        Then the brief still sends, with the hardcoded fallback focus line.

        Why this matters: same discipline as run_daily_briefing — if Gemma
        falls over, the deterministic facts still ship. Telegram never
        receives an empty message.
        """
        # Given
        import llm_config
        def boom(*a, **kw):
            raise RuntimeError("Ollama unavailable")
        monkeypatch.setattr(llm_config, "llm_generate", boom)

        # When
        orchestrator.run_saturday_review()

        # Then
        assert len(captured_telegram) == 1
        msg = captured_telegram[0]
        assert "NEXT WEEK" in msg
        assert "confluence" in msg.lower()  # the fallback text mentions confluence
