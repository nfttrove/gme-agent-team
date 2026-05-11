"""
Tests for the £5k progress calculator.

Style: behaviour-focused names + Given/When/Then docstrings (see tests/README.md).
The 'why this matters' line in each docstring names the real-world scenario the
test protects — these numbers feed the daily Telegram brief, so a regression
shows up in the team chat instantly.
"""
from datetime import date
from math import inf

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from target_progress import compute_progress, format_one_liner


class TestComputeProgress:

    def test_when_halfway_through_window_and_halfway_to_target_then_on_track(self):
        """
        Given the £5k target and a 150-day window (Jan 1 → May 31)
        When we are 75 days in and £2,500 earned
        Then on_track is True, pct_complete is 50, daily_burn matches remaining / remaining-days.

        This is the canonical 'no surprises' day — the brief should read 'on pace'.
        """
        # Given
        start = date(2026, 1, 1)
        deadline = date(2026, 5, 31)
        today = date(2026, 3, 17)  # ~75 days in

        # When
        p = compute_progress(
            realised_pnl_gbp=2500.0,
            target_gbp=5000.0,
            start_date=start,
            deadline=deadline,
            today=today,
        )

        # Then
        assert p.on_track is True
        assert p.pct_complete == 50.0
        assert p.days_left == (deadline - today).days
        assert p.daily_burn_gbp == 2500.0 / p.days_left

    def test_when_behind_linear_pace_then_on_track_is_false(self):
        """
        Given the £5k target with 20 days remaining of a 150-day window
        When we have earned only £1,000 (linear pace expected ~£4,330)
        Then on_track is False — the brief should read 'behind pace'.

        Matches the deadline-pressure scenario the user flagged: brief must
        surface when reality has diverged from plan.
        """
        # Given
        start = date(2026, 1, 1)
        deadline = date(2026, 5, 31)
        today = date(2026, 5, 11)  # 20 days left

        # When
        p = compute_progress(1000.0, start_date=start, deadline=deadline, today=today)

        # Then
        assert p.on_track is False
        assert p.days_left == 20
        assert p.daily_burn_gbp == 200.0  # (5000 - 1000) / 20

    def test_when_target_already_hit_then_daily_burn_is_zero_and_on_track_holds(self):
        """
        Given the £5k target and £6,000 earned
        When compute_progress runs at any point in the window
        Then daily_burn is 0 and on_track is True.

        The brief shouldn't shame the user into more trading after the goal is hit.
        """
        # Given / When
        p = compute_progress(
            realised_pnl_gbp=6000.0,
            today=date(2026, 4, 1),
        )

        # Then
        assert p.daily_burn_gbp == 0.0
        assert p.on_track is True
        assert p.pct_complete == 120.0

    def test_when_deadline_has_passed_and_short_then_days_left_is_zero_and_burn_is_infinite(self):
        """
        Given the deadline 2026-05-31
        When today is 2026-06-15 and we earned £3,000
        Then days_left is 0 and daily_burn_gbp is infinity.

        The brief's formatter must turn inf into 'deadline passed' (not '£inf/day').
        """
        # Given / When
        p = compute_progress(3000.0, today=date(2026, 6, 15))

        # Then
        assert p.days_left == 0
        assert p.daily_burn_gbp == inf
        assert p.on_track is False

    def test_when_today_is_before_start_date_then_on_track_is_true_regardless_of_earnings(self):
        """
        Given a window starting 2026-01-01
        When today is 2025-12-15 and we have earned nothing
        Then on_track is True — we haven't started counting yet.

        Edge case: if someone runs the brief before the official tracking start,
        the brief shouldn't claim we're already failing.
        """
        # Given / When
        p = compute_progress(
            realised_pnl_gbp=0.0,
            start_date=date(2026, 1, 1),
            today=date(2025, 12, 15),
        )

        # Then
        assert p.on_track is True

    def test_when_today_equals_deadline_and_target_hit_exactly_then_on_track_holds(self):
        """
        Given deadline 2026-05-31 and £5,000 earned to the penny
        When today is 2026-05-31
        Then on_track is True, days_left is 0, daily_burn is 0.

        Edge case at the boundary — the formatter should render 'target hit'.
        """
        p = compute_progress(5000.0, today=date(2026, 5, 31))
        assert p.on_track is True
        assert p.days_left == 0
        assert p.daily_burn_gbp == 0.0


class TestFormatOneLiner:

    def test_behind_pace_renders_with_burn_rate_and_status(self):
        """
        Given a TargetProgress that is behind pace with 20 days left
        When format_one_liner renders it
        Then the output contains the burn rate and the words 'behind pace'.
        """
        p = compute_progress(1000.0, today=date(2026, 5, 11))
        line = format_one_liner(p)
        assert "£1,000" in line
        assert "£5,000" in line
        assert "20 days left" in line
        assert "need £200/day" in line
        assert "behind pace" in line

    def test_target_hit_renders_without_burn_rate(self):
        """
        Given a TargetProgress where the target is hit
        When format_one_liner renders it
        Then it says 'target hit' and not 'need £X/day'.
        """
        p = compute_progress(5500.0, today=date(2026, 4, 1))
        line = format_one_liner(p)
        assert "target hit" in line
        assert "need £" not in line

    def test_deadline_passed_renders_without_infinity_symbol(self):
        """
        Given a TargetProgress where the deadline has passed and target unmet
        When format_one_liner renders it
        Then it says 'deadline passed', not '£inf/day'.

        Why this matters: 'inf' would leak Python's math.inf representation
        into a Telegram message — a real regression we don't want chat-visible.
        """
        p = compute_progress(3000.0, today=date(2026, 7, 1))
        line = format_one_liner(p)
        assert "deadline passed" in line
        assert "inf" not in line
