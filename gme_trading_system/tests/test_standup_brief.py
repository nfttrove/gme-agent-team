import sqlite3
from standup_brief import (
    categorize_agent,
    categorize_all,
    compute_status_diff,
    ensure_gate_history_table,
    save_gate_snapshot,
    load_previous_gate_snapshot,
    AgentVerdict,
)


def _acc(n, hit_rate, tp_rate=0.1):
    return {"n": n, "hit_rate": hit_rate, "tp_rate": tp_rate}


def test_categorize_listen_when_emit_and_above_bar():
    v = categorize_agent("Trendy", _acc(14, 0.79), "EMIT")
    assert v.bucket == "LISTEN"
    assert v.small_sample is True  # n < 20
    assert v.hits_correct == 11
    assert v.reason == "passing the bar"


def test_categorize_listen_no_small_sample_when_n_high():
    v = categorize_agent("Trendy", _acc(50, 0.65), "EMIT")
    assert v.bucket == "LISTEN"
    assert v.small_sample is False


def test_categorize_muted_when_suppressed_with_broken_reason():
    v = categorize_agent("Pattern Intraday", _acc(128, 0.20), "SUPPRESS")
    assert v.bucket == "MUTED"
    assert v.reason == "statistically broken"
    assert v.hits_correct == 26


def test_categorize_muted_when_shadowed_with_below_threshold_reason():
    v = categorize_agent("Futurist", _acc(34, 0.41), "SHADOW")
    assert v.bucket == "MUTED"
    assert v.reason == "below trust threshold"


def test_categorize_muted_when_emit_but_sample_too_small():
    v = categorize_agent("Pattern", _acc(4, 0.50), "EMIT")
    assert v.bucket == "MUTED"
    assert v.reason == "sample too small"


def test_categorize_all_sorts_trusted_and_muted_by_hit_rate():
    acc = {
        "Trendy": _acc(14, 0.79),
        "Pattern Intraday": _acc(128, 0.20),
        "Futurist": _acc(34, 0.41),
        "Pattern": _acc(10, 0.60),
    }
    gates = {
        "Trendy": "EMIT",
        "Pattern Intraday": "SUPPRESS",
        "Futurist": "SHADOW",
        "Pattern": "EMIT",
    }
    trusted, muted = categorize_all(acc, gates)

    assert [v.agent_name for v in trusted] == ["Trendy", "Pattern"]
    assert [v.agent_name for v in muted] == ["Futurist", "Pattern Intraday"]


def test_compute_status_diff_unchanged():
    today = [
        AgentVerdict("Trendy", "LISTEN", 14, 11, 0.79, 0.35, "passing", "EMIT", True),
        AgentVerdict("Futurist", "MUTED", 34, 14, 0.41, 0.12, "below", "SHADOW", False),
    ]
    diff = compute_status_diff(today, {"Trendy": "EMIT", "Futurist": "SHADOW"})
    assert diff == "unchanged since previous run"


def test_compute_status_diff_promotion_and_demotion():
    today = [
        AgentVerdict("Futurist", "LISTEN", 40, 22, 0.55, 0.20, "passing", "EMIT", False),
        AgentVerdict("Trendy", "MUTED", 16, 6, 0.38, 0.10, "below", "SHADOW", False),
    ]
    prev = {"Futurist": "SHADOW", "Trendy": "EMIT"}
    diff = compute_status_diff(today, prev)
    assert "Futurist promoted" in diff
    assert "Trendy dropped" in diff


def test_compute_status_diff_first_run_message():
    today = [AgentVerdict("Trendy", "LISTEN", 14, 11, 0.79, 0.35, "passing", "EMIT", True)]
    diff = compute_status_diff(today, {})
    assert "First snapshot" in diff


def test_gate_history_persistence_roundtrip(tmp_path):
    db = str(tmp_path / "gate.db")
    ensure_gate_history_table(db)
    verdicts_day1 = [
        AgentVerdict("Trendy", "LISTEN", 14, 11, 0.79, 0.35, "passing", "EMIT", True),
        AgentVerdict("Pattern Intraday", "MUTED", 128, 26, 0.20, 0.07, "broken", "SUPPRESS", False),
    ]
    save_gate_snapshot(db, verdicts_day1, snapshot_date="2026-05-15")

    verdicts_day2 = [
        AgentVerdict("Trendy", "LISTEN", 18, 13, 0.72, 0.40, "passing", "EMIT", True),
        AgentVerdict("Pattern Intraday", "MUTED", 130, 28, 0.22, 0.08, "broken", "SHADOW", False),
    ]
    save_gate_snapshot(db, verdicts_day2, snapshot_date="2026-05-16")

    previous = load_previous_gate_snapshot(db, "2026-05-16")
    assert previous == {"Trendy": "EMIT", "Pattern Intraday": "SUPPRESS"}


def test_format_standup_brief_renders_listen_muted_and_diff():
    from message_formatters_v2 import format_standup_brief
    trusted = [AgentVerdict("Trendy", "LISTEN", 14, 11, 0.79, 0.35, "passing the bar", "EMIT", True)]
    muted = [
        AgentVerdict("Pattern Intraday", "MUTED", 128, 25, 0.20, 0.07, "statistically broken", "SUPPRESS", False),
        AgentVerdict("Futurist", "MUTED", 34, 14, 0.41, 0.12, "below trust threshold", "SHADOW", False),
    ]
    msg = format_standup_brief(
        timestamp_et="12:32 ET",
        spot_price=21.60,
        trusted=trusted, muted=muted,
        last_24h_total=6, last_24h_wins=1, last_24h_avg_pnl_pct=6.4,
        status_diff="unchanged since previous run",
    )
    # Header doesn't double up "ET"
    assert "ET ET" not in msg
    # Listen block shows the trusted agent with caveat
    assert "LISTEN: Trendy" in msg
    assert "11/14" in msg and "79%" in msg
    assert "only agent passing" in msg
    assert "small sample" in msg
    # Muted block lists both with reasons
    assert "Pattern Intraday" in msg and "statistically broken" in msg
    assert "Futurist" in msg and "below trust threshold" in msg
    # 24h trades + diff
    assert "1 of 6" in msg and "+6.4%" in msg
    assert "unchanged since previous run" in msg


def test_format_standup_brief_handles_empty_trusted():
    from message_formatters_v2 import format_standup_brief
    msg = format_standup_brief(
        timestamp_et="12:32 ET", spot_price=21.60,
        trusted=[], muted=[],
        last_24h_total=0, last_24h_wins=0, last_24h_avg_pnl_pct=None,
        status_diff="First snapshot recorded — diff begins tomorrow",
    )
    assert "NO TRUSTED AGENTS" in msg
    assert "no paper trades closed" in msg


def test_save_gate_snapshot_is_idempotent_on_same_date(tmp_path):
    """Both the 11:00 and 16:00 runs save to the same date — second call must overwrite."""
    db = str(tmp_path / "gate.db")
    ensure_gate_history_table(db)
    v1 = [AgentVerdict("Trendy", "LISTEN", 10, 7, 0.70, 0.30, "passing", "EMIT", True)]
    v2 = [AgentVerdict("Trendy", "LISTEN", 12, 9, 0.75, 0.32, "passing", "EMIT", True)]
    save_gate_snapshot(db, v1, snapshot_date="2026-05-16")
    save_gate_snapshot(db, v2, snapshot_date="2026-05-16")

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT hit_rate, sample_size FROM agent_gate_history WHERE snapshot_date='2026-05-16'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 0.75
    assert rows[0][1] == 12
