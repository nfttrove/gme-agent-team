import sqlite3

from agent_coach import (
    resolve_agent_name,
    diagnose_agent,
    format_coach_report,
    _parse_diagnosis,
    _bucket_stats,
    CoachReport,
)


def _seed_signal_scores(db, rows):
    """Seed both signal_scores AND signal_alerts (confidence lives on the
    latter; agent_coach LEFT-JOINs them). Each row tuple shape:
    (agent_name, signal_type, confidence, directional_hit, tp_hit,
     baseline_price, end_price, validated_at)"""
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_scores (
                signal_id TEXT PRIMARY KEY,
                agent_name TEXT, signal_type TEXT,
                directional_hit INTEGER, tp_hit INTEGER,
                baseline_price REAL, end_price REAL, validated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS signal_alerts (
                id TEXT PRIMARY KEY,
                agent_name TEXT, signal_type TEXT, confidence REAL,
                entry_price REAL, stop_loss REAL, take_profit REAL,
                timestamp TEXT
            );
        """)
        for i, (agent, sig_type, conf, dir_hit, tp_hit, baseline, end, validated) in enumerate(rows):
            sig_id = f"sig-{i:04d}"
            conn.execute(
                "INSERT INTO signal_scores (signal_id, agent_name, signal_type, directional_hit, tp_hit, baseline_price, end_price, validated_at) VALUES (?,?,?,?,?,?,?,?)",
                (sig_id, agent, sig_type, dir_hit, tp_hit, baseline, end, validated),
            )
            conn.execute(
                "INSERT INTO signal_alerts (id, agent_name, signal_type, confidence, entry_price, stop_loss, take_profit, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (sig_id, agent, sig_type, conf, baseline, None, None, validated),
            )


def test_resolve_agent_name_exact_match_is_case_insensitive(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_signal_scores(db, [("Pattern Intraday", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:00:00")])
    assert resolve_agent_name(db, "pattern intraday") == "Pattern Intraday"
    assert resolve_agent_name(db, "Pattern Intraday") == "Pattern Intraday"


def test_resolve_agent_name_substring_when_unique(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_signal_scores(db, [
        ("Pattern Intraday", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:00:00"),
        ("Futurist", "BEAR", 0.7, 0, None, 21.0, 21.2, "2026-05-10T10:05:00"),
    ])
    assert resolve_agent_name(db, "futur") == "Futurist"


def test_resolve_agent_name_returns_none_for_ambiguous(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_signal_scores(db, [
        ("Pattern", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:00:00"),
        ("Pattern Intraday", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:05:00"),
    ])
    # "Pattern" matches both substrings → None (ambiguous)
    # but it ALSO exactly matches "Pattern" so exact-match wins
    assert resolve_agent_name(db, "Pattern") == "Pattern"
    # Pure substring ambiguity:
    assert resolve_agent_name(db, "pat") is None  # matches Pattern + Pattern Intraday


def test_resolve_agent_name_returns_none_for_no_match(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_signal_scores(db, [("Trendy", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:00:00")])
    assert resolve_agent_name(db, "nonsense") is None


def test_bucket_stats_separates_bull_and_bear():
    signals = [
        {"signal_type": "BULL", "directional_hit": 1},
        {"signal_type": "BULL", "directional_hit": 0},
        {"signal_type": "BEAR", "directional_hit": 1},
        {"signal_type": "BEAR", "directional_hit": 1},
        {"signal_type": "BULLISH", "directional_hit": 0},
    ]
    stats = _bucket_stats(signals)
    assert stats["n_bull"] == 3  # BULL + BULL + BULLISH
    assert stats["n_bear"] == 2
    assert round(stats["bull"], 2) == round(1 / 3, 2)
    assert round(stats["bear"], 2) == 1.0
    assert round(stats["overall"], 1) == 0.6  # 3 of 5


def test_diagnose_agent_too_few_signals_returns_not_ok(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_signal_scores(db, [
        ("Pattern", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:00:00"),
    ])
    report = diagnose_agent(db, "Pattern")
    assert report.ok is False
    assert "scored signals" in report.reason_if_failed


def test_diagnose_agent_unknown_agent_returns_not_ok(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_signal_scores(db, [
        ("Trendy", "BULL", 0.6, 1, None, 20.0, 20.5, "2026-05-10T10:00:00"),
    ])
    report = diagnose_agent(db, "Ghost")
    assert report.ok is False
    assert "no agent matches" in report.reason_if_failed


def test_diagnose_agent_happy_path_uses_stub_llm(tmp_path):
    db = str(tmp_path / "t.db")
    # 5 BULL signals, 3 right; 5 BEAR signals, 0 right (the agent is wrong on BEAR)
    rows = []
    for i in range(5):
        rows.append(("Pattern Intraday", "BULL", 0.65, 1 if i < 3 else 0, None, 20.0, 20.0 + (i + 1) * 0.1, f"2026-05-10T10:0{i}:00"))
    for i in range(5):
        rows.append(("Pattern Intraday", "BEAR", 0.70, 0, None, 21.0, 21.0 + (i + 1) * 0.1, f"2026-05-11T10:0{i}:00"))
    _seed_signal_scores(db, rows)

    captured_prompt = []
    def fake_llm(prompt, model, num_predict, temperature):
        captured_prompt.append(prompt)
        return (
            "PATTERN:\n"
            "BEAR signals are 0% accurate over the recent window while BULL is 60%. "
            "The agent is calling reversals as bearish when the underlying trend is up.\n\n"
            "HYPOTHESIS:\n"
            "Likely reading short-term pullbacks in an uptrend as new bear signals.\n\n"
            "SUGGESTION:\n"
            "Require trend confirmation before emitting BEAR — check 21d EMA slope."
        )

    report = diagnose_agent(db, "pattern intraday", llm_caller=fake_llm)
    assert report.ok is True
    assert report.agent_name == "Pattern Intraday"
    assert report.sample_size == 10
    assert round(report.bull_hit_rate, 2) == 0.6
    assert round(report.bear_hit_rate, 2) == 0.0
    assert "BEAR signals are 0%" in report.diagnosis
    assert "Likely cause:" in report.diagnosis
    assert "trend confirmation" in report.suggestion
    # The prompt should include the stats so Pro has context
    assert "Pattern Intraday" in captured_prompt[0]
    assert "BULL signals: 60%" in captured_prompt[0]
    assert "BEAR signals: 0%" in captured_prompt[0]


def test_diagnose_agent_llm_failure_returns_not_ok(tmp_path):
    db = str(tmp_path / "t.db")
    rows = [("Trendy", "BULL", 0.7, 1, None, 20.0, 21.0, f"2026-05-10T10:0{i}:00") for i in range(6)]
    _seed_signal_scores(db, rows)

    def boom(*a, **kw):
        raise RuntimeError("API timeout")

    report = diagnose_agent(db, "Trendy", llm_caller=boom)
    assert report.ok is False
    assert "Gemini Pro call failed" in report.reason_if_failed
    # Stats should still be populated even though LLM failed
    assert report.overall_hit_rate == 1.0
    assert report.sample_size == 6


def test_parse_diagnosis_handles_well_formed_response():
    raw = (
        "PATTERN:\n"
        "Wrong on BEAR consistently.\n\n"
        "HYPOTHESIS:\n"
        "Reading pullbacks as reversals.\n\n"
        "SUGGESTION:\n"
        "Add trend filter."
    )
    diagnosis, suggestion = _parse_diagnosis(raw)
    assert diagnosis == "Wrong on BEAR consistently.\n\nLikely cause: Reading pullbacks as reversals."
    assert suggestion == "Add trend filter."


def test_format_coach_report_renders_failure_with_reason():
    report = CoachReport(ok=False, agent_name="Ghost", reason_if_failed="no agent matches 'Ghost'")
    msg = format_coach_report(report)
    assert "COACH: Ghost" in msg
    assert "⚠️" in msg
    assert "no agent matches" in msg


def test_format_coach_report_renders_full_report():
    report = CoachReport(
        ok=True, agent_name="Pattern Intraday", sample_size=10,
        overall_hit_rate=0.30, bull_hit_rate=0.60, bear_hit_rate=0.0,
        diagnosis="BEAR wrong every time.\n\nLikely cause: misread pullbacks.",
        suggestion="Add trend filter.",
    )
    msg = format_coach_report(report)
    assert "COACH: Pattern Intraday" in msg
    assert "10 resolved signals" in msg
    assert "30% overall" in msg and "BULL 60%" in msg and "BEAR 0%" in msg
    assert "Diagnosis" in msg
    assert "BEAR wrong every time" in msg
    assert "Add trend filter" in msg
