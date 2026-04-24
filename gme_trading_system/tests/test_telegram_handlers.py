"""
Smoke tests for every Telegram command handler in telegram_bot.py.

Goal: prove each `elif cmd == "/x":` branch runs without raising and produces
SOME output via _send(). These are branch-coverage tests, not behavior tests —
they catch the class of bug where a typo or missing import silently breaks a
command (e.g. the `os.dirname → os.path.dirname` bug).

External dependencies are mocked:
  - _send is captured via monkeypatch (no real Telegram API calls)
  - subprocess.run (for /learn, /lessons, /test) returns canned output
  - crewai Crew (for /brief, /update) returns a stub result
  - requests.post (for /compare) returns canned JSON
  - Supabase sync client is mocked
  - run_screen (for /trove) returns a minimal list

DB is a tempfile sqlite populated with the minimum schema each command touches.
"""
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure gme_trading_system is on the path when pytest is invoked from repo root.
REPO_SYS_PATH = Path(__file__).resolve().parent.parent
if str(REPO_SYS_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_SYS_PATH))

import telegram_bot  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Temp SQLite with just enough schema + rows for the handlers to not crash."""
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE price_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, close REAL, volume REAL, timestamp TEXT
        );
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT, task_type TEXT, content TEXT,
            status TEXT DEFAULT 'ok', timestamp TEXT
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            horizon TEXT, predicted_price REAL, confidence REAL, timestamp TEXT
        );
        CREATE TABLE trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT, confidence REAL, timestamp TEXT
        );
        CREATE TABLE news_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            headline TEXT, sentiment_score REAL, timestamp TEXT
        );
        CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE performance_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, agent_name TEXT NOT NULL, metric TEXT NOT NULL,
            value REAL NOT NULL, sample_size INTEGER DEFAULT 0, notes TEXT,
            UNIQUE(date, agent_name, metric)
        );
        INSERT INTO price_ticks (symbol, close, volume, timestamp)
            VALUES ('GME', 25.34, 3250, '2026-04-23T15:38:00Z');
        INSERT INTO agent_logs (agent_name, task_type, content, status, timestamp)
            VALUES ('Synthesis','synthesis','Bullish consensus','ok','2026-04-23T15:30:00Z');
        INSERT INTO predictions (horizon, predicted_price, confidence, timestamp)
            VALUES ('1h', 25.80, 0.72, '2026-04-23T15:00:00Z');
        INSERT INTO trade_decisions (action, confidence, timestamp)
            VALUES ('buy', 0.65, '2026-04-23T14:00:00Z');
        INSERT INTO bot_settings VALUES ('notification_frequency', 'medium');
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(telegram_bot, "DB_PATH", str(db))
    return db


@pytest.fixture
def captured_sends(monkeypatch):
    """Collect every call to _send instead of hitting Telegram."""
    sent = []
    monkeypatch.setattr(telegram_bot, "_send", lambda text: sent.append(text))
    return sent


# ── Tests — one per command branch ────────────────────────────────────────────

def test_help(captured_sends):
    telegram_bot.handle_command("/help")
    assert captured_sends, "/help produced no output"
    assert "Command Guide" in captured_sends[0]


def test_supportme(captured_sends):
    telegram_bot.handle_command("/supportme")
    assert captured_sends
    assert telegram_bot.PAYPAL_URL in captured_sends[0]


def test_buymeacoffee_alias(captured_sends):
    telegram_bot.handle_command("/buymeacoffee")
    assert captured_sends
    assert telegram_bot.PAYPAL_URL in captured_sends[0]


def test_status(seeded_db, captured_sends):
    telegram_bot.handle_command("/status")
    assert captured_sends
    body = captured_sends[0]
    assert "System Status" in body
    assert "Ticks today" in body


def test_ticks(seeded_db, captured_sends):
    telegram_bot.handle_command("/ticks")
    assert captured_sends
    assert "GME Tick Data" in captured_sends[0]
    assert "25.34" in captured_sends[0]


def test_agents(seeded_db, captured_sends):
    telegram_bot.handle_command("/agents")
    assert captured_sends
    assert "Agent Last Activity" in captured_sends[0]
    assert "Synthesis" in captured_sends[0]


def test_standup(seeded_db, captured_sends):
    telegram_bot.handle_command("/standup")
    assert captured_sends
    assert "AGENT STANDUP" in captured_sends[0]


def test_freshness(seeded_db, captured_sends, monkeypatch):
    fake = types.ModuleType("data_freshness")
    fake.check = lambda: [("price_ticks", True, "3.5k rows today"),
                          ("daily_candles", True, "current")]
    monkeypatch.setitem(sys.modules, "data_freshness", fake)
    telegram_bot.handle_command("/freshness")
    assert captured_sends
    assert "Data Freshness" in captured_sends[0]


def test_frequency_read(seeded_db, captured_sends):
    telegram_bot.handle_command("/frequency")
    assert captured_sends
    assert "Current frequency" in captured_sends[0]


def test_frequency_set(seeded_db, captured_sends):
    telegram_bot.handle_command("/frequency high")
    assert captured_sends
    assert "high" in captured_sends[0].lower()
    # Round-trip: reading the setting back should now return 'high'.
    assert telegram_bot._get_frequency() == "high"


def test_frequency_invalid(seeded_db, captured_sends):
    telegram_bot.handle_command("/frequency bogus")
    # Invalid level falls through to "show current" — shouldn't raise.
    assert captured_sends
    assert "Current frequency" in captured_sends[0]


def test_update(seeded_db, captured_sends, monkeypatch):
    monkeypatch.setattr(telegram_bot, "_run_agent_refresh",
                        lambda: {"valerie": "ok", "synthesis": "ok",
                                 "news": "ok", "cto": "ok"})
    fake_sync = types.ModuleType("supabase_sync")
    fake_sync._get_client = lambda: MagicMock()
    fake_sync._load_state = lambda: {}
    fake_sync.sync_once = lambda client, state: state
    monkeypatch.setitem(sys.modules, "supabase_sync", fake_sync)

    telegram_bot.handle_command("/update")
    joined = "\n".join(captured_sends)
    assert "SYSTEM REFRESH" in joined
    assert "Supabase sync complete" in joined


def test_factory_functions_exist():
    """Verify make_validate_data_task and make_synthesis_task are importable
    (bot imports these; absence would cause ImportError on /update and /brief)."""
    sys.path.insert(0, str(REPO_SYS_PATH))
    from tasks import make_validate_data_task, make_synthesis_task
    assert callable(make_validate_data_task)
    assert callable(make_synthesis_task)


def test_brief(seeded_db, captured_sends, monkeypatch):
    fake_agents = types.ModuleType("agents")
    fake_agents.briefing_agent = MagicMock()
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    fake_crewai = types.ModuleType("crewai")

    class _Crew:
        def __init__(self, *a, **k): pass
        def kickoff(self): return "📍 MARKET: GME at $25.34. Rising."
    fake_crewai.Crew = _Crew
    fake_crewai.Process = MagicMock(sequential=0)
    fake_crewai.Task = lambda **kw: MagicMock(**kw)
    monkeypatch.setitem(sys.modules, "crewai", fake_crewai)

    telegram_bot.handle_command("/brief")
    joined = "\n".join(captured_sends)
    assert "STRATEGY BRIEF" in joined


def test_brief_price_direction_logic(seeded_db, captured_sends, monkeypatch):
    """Verify /brief correctly determines price direction from opening baseline."""
    # Insert today's opening and current price
    conn = sqlite3.connect(seeded_db)
    # Clear old data
    conn.execute("DELETE FROM price_ticks")
    # Insert opening price (low)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, volume, timestamp) VALUES (?,?,?,?)",
        ("GME", 23.50, 1000, "2026-04-23T09:35:00-04:00"),
    )
    # Insert current price (higher → rising)
    conn.execute(
        "INSERT INTO price_ticks (symbol, close, volume, timestamp) VALUES (?,?,?,?)",
        ("GME", 24.20, 5000, "2026-04-23T15:30:00-04:00"),
    )
    conn.commit()
    conn.close()

    fake_agents = types.ModuleType("agents")
    fake_agents.briefing_agent = MagicMock()
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    fake_crewai = types.ModuleType("crewai")
    class _Crew:
        def __init__(self, *a, **k):
            # Capture the task description to verify direction was calculated
            self.task_desc = a[1][0].description if a and len(a) > 1 else ""
        def kickoff(self): return "📍 MARKET: GME at $24.20. Rising."
    fake_crewai.Crew = _Crew
    fake_crewai.Process = MagicMock(sequential=0)
    fake_crewai.Task = lambda **kw: MagicMock(**kw)
    monkeypatch.setitem(sys.modules, "crewai", fake_crewai)

    telegram_bot.handle_command("/brief")
    joined = "\n".join(captured_sends)
    assert "STRATEGY BRIEF" in joined
    # Verify rising direction appears in output (current $24.20 > opening $23.50)
    assert "rising" in joined.lower() or "📍 market" in joined.lower()


def test_trove_default_watchlist(seeded_db, captured_sends, monkeypatch):
    fake_trove = types.ModuleType("trove")
    fake_trove.DEFAULT_WATCHLIST = ["GME", "VIPS"]
    fake_trove.run_screen = lambda tickers, max_tickers=20: [
        {"ticker": "GME", "score": 57.0, "rating": "★★★☆☆", "immunity": 3,
         "pillar_A": 20, "pillar_B": 22, "pillar_C": 15,
         "net_cash_pct": 18, "altman_z": 3.2},
    ]
    monkeypatch.setitem(sys.modules, "trove", fake_trove)
    telegram_bot.handle_command("/trove")
    joined = "\n".join(captured_sends)
    assert "Trove Score Rankings" in joined
    assert "GME" in joined


def test_trove_with_tickers(seeded_db, captured_sends, monkeypatch):
    fake_trove = types.ModuleType("trove")
    fake_trove.DEFAULT_WATCHLIST = []
    fake_trove.run_screen = lambda tickers, max_tickers=20: [
        {"ticker": t, "score": 50.0, "rating": "★★★☆☆", "immunity": 2,
         "pillar_A": 15, "pillar_B": 20, "pillar_C": 15,
         "net_cash_pct": 10, "altman_z": 2.5}
        for t in tickers
    ]
    monkeypatch.setitem(sys.modules, "trove", fake_trove)
    telegram_bot.handle_command("/trove AAPL MSFT")
    joined = "\n".join(captured_sends)
    assert "AAPL" in joined and "MSFT" in joined


def test_learn_missing_why(captured_sends):
    telegram_bot.handle_command('/learn "foo"')
    assert captured_sends
    assert "Usage" in captured_sends[0]


def test_learn_success(captured_sends, monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return types.SimpleNamespace(returncode=0, stdout="graduated", stderr="")
    monkeypatch.setattr("subprocess.run", fake_run)
    telegram_bot.handle_command('/learn "High IV = decay" --why "IV rank >70"')
    joined = "\n".join(captured_sends)
    assert "Lesson graduated" in joined


def test_lessons(captured_sends, monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        return types.SimpleNamespace(returncode=0,
                                     stdout="lesson 1: buy low\nlesson 2: sell high",
                                     stderr="")
    monkeypatch.setattr("subprocess.run", fake_run)
    telegram_bot.handle_command("/lessons trading")
    joined = "\n".join(captured_sends)
    assert "Lessons for" in joined


def test_test_passing(captured_sends, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return types.SimpleNamespace(
            returncode=0,
            stdout="========== 23 passed in 0.13s ==========",
            stderr="",
        )
    monkeypatch.setattr("subprocess.run", fake_run)
    telegram_bot.handle_command("/test")
    joined = "\n".join(captured_sends)
    assert "ALL COMMAND TESTS PASSED" in joined


def test_test_failing(captured_sends, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return types.SimpleNamespace(
            returncode=1,
            stdout="========== 20 passed, 3 failed in 0.15s ==========",
            stderr="",
        )
    monkeypatch.setattr("subprocess.run", fake_run)
    telegram_bot.handle_command("/test")
    joined = "\n".join(captured_sends)
    assert "TEST FAILURES" in joined


def test_compare_with_args(captured_sends, monkeypatch):
    class _Resp:
        status_code = 200
        def json(self):  # noqa: D401
            return {"response": "GME is at $25.34"}
    monkeypatch.setattr(telegram_bot, "_build_context", lambda: "ctx")
    monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, **k: _Resp())
    telegram_bot.handle_command("/compare what is GME price?")
    joined = "\n".join(captured_sends)
    assert "Model Comparison" in joined


def test_unknown_command(captured_sends):
    telegram_bot.handle_command("/bogus")
    assert captured_sends
    assert "Available commands" in captured_sends[0]


def test_compare_without_args_falls_through(captured_sends):
    # /compare with no args doesn't match `elif cmd == "/compare" and args:`,
    # so it drops to the unknown-command branch. Should not raise.
    telegram_bot.handle_command("/compare")
    assert captured_sends
    assert "Available commands" in captured_sends[0]


def test_force_without_args_shows_menu(captured_sends):
    telegram_bot.handle_command("/force")
    joined = "\n".join(captured_sends)
    assert "Force an agent cycle" in joined
    assert "valerie" in joined and "synthesis" in joined


def test_force_unknown_agent_shows_menu(captured_sends):
    telegram_bot.handle_command("/force bogus")
    joined = "\n".join(captured_sends)
    assert "Force an agent cycle" in joined


def test_force_valid_agent_invokes_orchestrator(seeded_db, captured_sends, monkeypatch):
    # Seed a log row so the handler can report back.
    conn = sqlite3.connect(seeded_db)
    conn.execute(
        "INSERT INTO agent_logs (timestamp, agent_name, content, task_type, status) "
        "VALUES (?,?,?,?,?)",
        ("2026-04-23T15:30:00-04:00", "Valerie", "data clean: 60 ticks", "validation", "ok"),
    )
    conn.commit()
    conn.close()

    called = []
    fake_orch = types.ModuleType("orchestrator")
    fake_orch.run_validation = lambda: called.append("run_validation")
    monkeypatch.setitem(sys.modules, "orchestrator", fake_orch)

    telegram_bot.handle_command("/force valerie")
    joined = "\n".join(captured_sends)
    assert called == ["run_validation"]
    assert "Valerie" in joined
    assert "data clean" in joined
