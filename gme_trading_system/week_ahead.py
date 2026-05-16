"""Sunday week-ahead preview — pure functions over dates + DB snapshots.

The Sunday brief is the one-ping calendar setup for the week, deliberately
small. Holds the trading-day math and the snapshot loaders; the orchestrator
glues these to the notifier.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from market_hours import _holidays

EARNINGS_HORIZON_DAYS = 30
DEADLINE_DATE = date(2026, 5, 31)  # user's £5k target — flagged in auto-memory


@dataclass(frozen=True)
class WeekAheadSnapshot:
    today: date
    next_friday: date
    last_max_pain: float | None
    last_spot_at_snapshot: float | None
    last_oi_bias: str | None
    last_snapshot_expiration: str | None
    next_earnings_date: str | None
    earnings_days_away: int | None
    trading_days_this_week: int
    trading_days_to_deadline: int | None  # None once we're past it


def next_friday_on_or_after(today: date) -> date:
    """The upcoming Friday, or today if today *is* Friday."""
    offset = (4 - today.weekday()) % 7
    return today + timedelta(days=offset)


def trading_days_between(start: date, end: date) -> int:
    """Mon-Fri count between start (exclusive) and end (inclusive), minus US holidays."""
    if end <= start:
        return 0
    holidays: set[date] = set()
    for year in range(start.year, end.year + 1):
        holidays |= _holidays(year)
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5 and cur not in holidays:
            count += 1
        cur += timedelta(days=1)
    return count


def _load_latest_options_snapshot(db_path: str) -> tuple[str | None, float | None, float | None, str | None]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """SELECT expiration, max_pain_strike, current_price, net_oi_bias
                 FROM options_snapshots ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]


def _load_next_earnings(db_path: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(
                """SELECT next_earnings_date FROM market_fundamentals
                   WHERE next_earnings_date IS NOT NULL
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return row[0] if row else None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def build_week_ahead_snapshot(db_path: str, today: date | None = None) -> WeekAheadSnapshot:
    today = today or date.today()
    friday = next_friday_on_or_after(today)
    exp, max_pain, spot, bias = _load_latest_options_snapshot(db_path)
    earnings_raw = _load_next_earnings(db_path)
    earnings_dt = _parse_iso_date(earnings_raw)
    earnings_days = (earnings_dt - today).days if earnings_dt else None

    days_to_deadline = trading_days_between(today, DEADLINE_DATE) if today <= DEADLINE_DATE else None

    return WeekAheadSnapshot(
        today=today,
        next_friday=friday,
        last_max_pain=max_pain,
        last_spot_at_snapshot=spot,
        last_oi_bias=bias,
        last_snapshot_expiration=exp,
        next_earnings_date=earnings_raw,
        earnings_days_away=earnings_days,
        trading_days_this_week=trading_days_between(today, friday),
        trading_days_to_deadline=days_to_deadline,
    )
