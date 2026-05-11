"""
£5k-by-2026-05-31 progress calculator.

This is a *private* signal — surfaced on demand via the /progress Telegram
command, never in the broadcast briefs. The team chat stays focused on
trading; the operator checks progress privately.

Pure module: no DB, no network, no IO. The caller passes realised PnL in GBP
and the function returns a `TargetProgress` snapshot. Kept pure so tests
stay fast and the helper can be reused from any caller (telegram_bot, future
internal-only digests, learning hooks).
"""
import os
from dataclasses import dataclass
from datetime import date
from math import inf


TARGET_GBP_DEFAULT = 5000.0
START_DATE_DEFAULT = date(2026, 1, 1)
DEADLINE_DEFAULT = date(2026, 5, 31)

# Rough USD→GBP for converting trade_decisions.pnl (USD) into the target unit.
# Override via env when a live FX feed lands.
USD_GBP_RATE = float(os.getenv("USD_GBP_RATE", "0.79"))


@dataclass(frozen=True)
class TargetProgress:
    earned_gbp: float
    target_gbp: float
    days_left: int          # 0 if deadline has passed
    daily_burn_gbp: float   # GBP needed per remaining day to hit target; inf if deadline passed and short
    pct_complete: float     # 0–100+ (can exceed 100 if over-target)
    on_track: bool          # True if earned >= linear-pace expectation for today


def compute_progress(
    realised_pnl_gbp: float,
    target_gbp: float = TARGET_GBP_DEFAULT,
    start_date: date = START_DATE_DEFAULT,
    deadline: date = DEADLINE_DEFAULT,
    today: date | None = None,
) -> TargetProgress:
    today = today or date.today()

    days_left = max(0, (deadline - today).days)
    remaining = max(0.0, target_gbp - realised_pnl_gbp)

    if remaining <= 0:
        daily_burn = 0.0
    elif days_left == 0:
        daily_burn = inf
    else:
        daily_burn = remaining / days_left

    pct_complete = (realised_pnl_gbp / target_gbp * 100) if target_gbp else 0.0

    on_track = _is_on_track(realised_pnl_gbp, target_gbp, start_date, deadline, today)

    return TargetProgress(
        earned_gbp=realised_pnl_gbp,
        target_gbp=target_gbp,
        days_left=days_left,
        daily_burn_gbp=daily_burn,
        pct_complete=pct_complete,
        on_track=on_track,
    )


def _is_on_track(
    earned: float,
    target: float,
    start_date: date,
    deadline: date,
    today: date,
) -> bool:
    if today <= start_date:
        return True
    if today >= deadline:
        return earned >= target

    elapsed = (today - start_date).days
    total = (deadline - start_date).days
    expected = target * (elapsed / total) if total else target
    return earned >= expected


def format_one_liner(p: TargetProgress) -> str:
    """Single-line Telegram-friendly rendering used by the daily brief."""
    if p.daily_burn_gbp == inf:
        burn = "deadline passed"
    elif p.daily_burn_gbp == 0:
        burn = "target hit"
    else:
        burn = f"need £{p.daily_burn_gbp:.0f}/day"

    pace = "on pace" if p.on_track else "behind pace"
    return (
        f"£{p.earned_gbp:,.0f} / £{p.target_gbp:,.0f} "
        f"({p.pct_complete:.0f}%) — {p.days_left} days left — {burn} — {pace}"
    )
