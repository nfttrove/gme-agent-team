import sqlite3
from datetime import date

import pytest

from week_ahead import (
    next_friday_on_or_after,
    trading_days_between,
    build_week_ahead_snapshot,
)


def test_next_friday_returns_today_when_today_is_friday():
    assert next_friday_on_or_after(date(2026, 5, 22)) == date(2026, 5, 22)


def test_next_friday_returns_upcoming_friday_for_sunday():
    assert next_friday_on_or_after(date(2026, 5, 17)) == date(2026, 5, 22)


def test_next_friday_returns_upcoming_friday_for_thursday():
    assert next_friday_on_or_after(date(2026, 5, 21)) == date(2026, 5, 22)


def test_trading_days_between_excludes_weekends():
    # Sun 2026-05-17 → Fri 2026-05-22 = Mon, Tue, Wed, Thu, Fri = 5
    assert trading_days_between(date(2026, 5, 17), date(2026, 5, 22)) == 5


def test_trading_days_between_excludes_memorial_day_holiday():
    # Sun 2026-05-24 → Fri 2026-05-29 would be 5 weekdays, but Memorial Day
    # 2026 falls on Mon 2026-05-25 — should drop to 4.
    assert trading_days_between(date(2026, 5, 24), date(2026, 5, 29)) == 4


def test_trading_days_between_returns_zero_when_end_not_after_start():
    assert trading_days_between(date(2026, 5, 22), date(2026, 5, 22)) == 0
    assert trading_days_between(date(2026, 5, 22), date(2026, 5, 18)) == 0


@pytest.fixture
def stub_db(tmp_path):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE options_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
                expiration TEXT, max_pain_strike REAL, current_price REAL,
                delta_to_max_pain REAL, call_oi_total INTEGER, put_oi_total INTEGER,
                put_call_ratio REAL, net_oi_bias TEXT
            );
            CREATE TABLE market_fundamentals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, next_earnings_date TEXT
            );
            INSERT INTO options_snapshots (timestamp, expiration, max_pain_strike, current_price, net_oi_bias)
                VALUES ('2026-05-11T08:30:00-04:00', '2026-05-15', 24.0, 24.28, 'calls');
            INSERT INTO market_fundamentals (next_earnings_date) VALUES ('2026-06-09');
        """)
    return str(db)


def test_snapshot_populates_all_fields_from_real_schema(stub_db):
    snap = build_week_ahead_snapshot(stub_db, today=date(2026, 5, 17))

    assert snap.next_friday == date(2026, 5, 22)
    assert snap.last_max_pain == 24.0
    assert snap.last_spot_at_snapshot == 24.28
    assert snap.last_oi_bias == "calls"
    assert snap.last_snapshot_expiration == "2026-05-15"
    assert snap.next_earnings_date == "2026-06-09"
    assert snap.earnings_days_away == 23
    assert snap.trading_days_this_week == 5
    assert snap.trading_days_to_deadline == 9  # Mon 5/18 through Fri 5/29 minus Memorial Day


def test_snapshot_returns_none_for_deadline_after_passed(stub_db):
    snap = build_week_ahead_snapshot(stub_db, today=date(2026, 6, 1))
    assert snap.trading_days_to_deadline is None


def test_snapshot_handles_missing_options_snapshot(tmp_path):
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE options_snapshots (id INTEGER PRIMARY KEY, expiration TEXT,
                max_pain_strike REAL, current_price REAL, net_oi_bias TEXT);
            CREATE TABLE market_fundamentals (id INTEGER PRIMARY KEY, next_earnings_date TEXT);
        """)

    snap = build_week_ahead_snapshot(str(db), today=date(2026, 5, 17))

    assert snap.last_max_pain is None
    assert snap.next_earnings_date is None
    assert snap.next_friday == date(2026, 5, 22)
