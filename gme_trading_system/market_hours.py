"""
US equity market hours guard.
NYSE: 09:30–16:00 ET, Monday–Friday, excluding federal holidays.
"""
from datetime import date, datetime, time
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

# Federal holidays that close NYSE (simplified — covers major ones)
_FIXED_HOLIDAYS = {
    (1, 1),   # New Year's Day
    (7, 4),   # Independence Day
    (12, 25), # Christmas
}

# Observed rule: if a holiday falls on Saturday, observed Friday; Sunday → Monday.
def _observed(year: int, month: int, day: int) -> date:
    d = date(year, month, day)
    if d.weekday() == 5:  # Saturday → Friday
        from datetime import timedelta
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday → Monday
        from datetime import timedelta
        return d + timedelta(days=1)
    return d


def _holidays(year: int) -> set[date]:
    holidays = {_observed(year, m, d) for m, d in _FIXED_HOLIDAYS}

    # MLK Day — 3rd Monday in January
    holidays.add(_nth_weekday(year, 1, 0, 3))
    # Presidents Day — 3rd Monday in February
    holidays.add(_nth_weekday(year, 2, 0, 3))
    # Memorial Day — last Monday in May
    holidays.add(_last_weekday(year, 5, 0))
    # Labor Day — 1st Monday in September
    holidays.add(_nth_weekday(year, 9, 0, 1))
    # Thanksgiving — 4th Thursday in November
    holidays.add(_nth_weekday(year, 11, 3, 4))
    return holidays


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d = date(year, month, d.day + 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != weekday:
        from datetime import timedelta
        d -= timedelta(days=1)
    return d


MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_market_open(dt: datetime | None = None) -> bool:
    """Return True if US equity markets are currently open."""
    now_et = (dt or datetime.now(ET)).astimezone(ET)
    today  = now_et.date()

    if today.weekday() >= 5:           # Weekend
        return False
    if today in _holidays(today.year): # Holiday
        return False
    t = now_et.time().replace(tzinfo=None)
    return MARKET_OPEN <= t < MARKET_CLOSE


def market_hours_required(func):
    """Decorator: skip function and log if outside market hours."""
    import logging
    log = logging.getLogger(__name__)

    def wrapper(*args, **kwargs):
        if not is_market_open():
            now_et = datetime.now(ET).strftime("%H:%M ET %a")
            log.info(f"[market_hours] {func.__name__} skipped — market closed ({now_et})")
            return None
        return func(*args, **kwargs)
    return wrapper
