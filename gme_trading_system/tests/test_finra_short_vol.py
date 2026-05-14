"""FINRA short-vol fetcher + cache + summary contract.

Verifies parsing, cache idempotency, weekend skipping, soft-fail on 404,
and the summary shape consumed by the CTO DV burst.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finra_short_vol import (  # noqa: E402
    _fetch_one_day,
    _parse_for_ticker,
    format_brief_line,
    get_short_vol_summary,
    update_for_ticker,
)


# Sample FINRA CNMS file content (real shape, GME row from 2026-05-13)
_SAMPLE_FILE = """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market

20260513|A|512419.941846|11|911764.924355|B,Q,N
20260513|GME|2027553.691119|2187|3523437.969979|B,Q,N
20260513|TSLA|45000000|100|110000000|B,Q,N
"""


class _OK:
    status_code = 200
    def __init__(self, text):
        self.text = text


class _NotFound:
    status_code = 404
    text = "Not Found"


class TestParser:
    def test_parses_known_ticker(self):
        result = _parse_for_ticker(_SAMPLE_FILE, "GME")
        assert result == (2027553, 3523437)

    def test_returns_none_for_missing_ticker(self):
        assert _parse_for_ticker(_SAMPLE_FILE, "ZZZZ") is None

    def test_case_insensitive(self):
        assert _parse_for_ticker(_SAMPLE_FILE, "gme") == (2027553, 3523437)

    def test_total_zero_returns_none(self):
        bad = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260513|XXX|100|0|0|B"
        assert _parse_for_ticker(bad, "XXX") is None


class TestFetcher:
    def test_404_returns_none(self):
        with patch("finra_short_vol.requests.get", return_value=_NotFound()):
            assert _fetch_one_day("20260101") is None

    def test_non_finra_html_rejected(self):
        """If the breaker returns a non-FINRA HTML page (e.g. error page),
        soft-fail rather than try to parse it as pipe-delimited data."""
        bogus = _OK("<html>oops</html>")
        with patch("finra_short_vol.requests.get", return_value=bogus):
            assert _fetch_one_day("20260101") is None

    def test_well_formed_returns_text(self):
        with patch("finra_short_vol.requests.get", return_value=_OK(_SAMPLE_FILE)):
            text = _fetch_one_day("20260513")
            assert text is not None
            assert "GME" in text


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


class TestUpdateForTicker:
    def test_inserts_rows_when_fresh(self, tmp_db):
        with patch("finra_short_vol._fetch_one_day", return_value=_SAMPLE_FILE):
            inserted = update_for_ticker("GME", days_back=5, db_path=tmp_db)
        # 5 calendar days back, weekends skipped → ≤ 5 weekday rows inserted.
        assert inserted >= 1
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute("SELECT COUNT(*) FROM finra_short_vol WHERE ticker='GME'").fetchone()
        conn.close()
        assert rows[0] == inserted

    def test_idempotent_second_run_inserts_nothing(self, tmp_db):
        """Re-running should hit the cache check and skip every fetch."""
        with patch("finra_short_vol._fetch_one_day", return_value=_SAMPLE_FILE):
            first = update_for_ticker("GME", days_back=5, db_path=tmp_db)

        # Second run: fetcher should NOT be called even once because cache hits
        call_count = [0]
        def counting_fetch(_yyyymmdd):
            call_count[0] += 1
            return _SAMPLE_FILE
        with patch("finra_short_vol._fetch_one_day", side_effect=counting_fetch):
            second = update_for_ticker("GME", days_back=5, db_path=tmp_db)
        assert second == 0
        assert call_count[0] == 0

    def test_skips_weekends_without_fetching(self, tmp_db):
        """Saturday/Sunday should never trigger an HTTP call."""
        # Fix today as a Wednesday so we can predict which days are weekends in the window
        fetch_dates = []
        def record_fetch(yyyymmdd):
            fetch_dates.append(yyyymmdd)
            return _SAMPLE_FILE
        with patch("finra_short_vol.date") as mock_date:
            mock_date.today.return_value = date(2026, 5, 13)  # a Wednesday
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            with patch("finra_short_vol._fetch_one_day", side_effect=record_fetch):
                update_for_ticker("GME", days_back=10, db_path=tmp_db)
        # Days_back=10 covers Sun May 3 through Tue May 12. Of those 10 days,
        # 2 are weekend days (May 3 Sun, May 9 Sat, May 10 Sun) → 7 weekdays.
        # Verify NO weekend dates were ever fetched.
        for ymd in fetch_dates:
            d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
            assert d.weekday() < 5, f"weekend {d} ({d.strftime('%A')}) should not have been fetched"


class TestSummary:
    def test_returns_none_when_no_data(self, tmp_db):
        # Force fetcher to return None (FINRA unreachable)
        with patch("finra_short_vol._fetch_one_day", return_value=None):
            summary = get_short_vol_summary("GME", db_path=tmp_db)
        assert summary is None

    def test_summary_shape(self, tmp_db):
        with patch("finra_short_vol._fetch_one_day", return_value=_SAMPLE_FILE):
            summary = get_short_vol_summary("GME", db_path=tmp_db)
        assert summary is not None
        # GME in sample: 2,027,553 / 3,523,437 = ~0.5754
        assert abs(summary["latest_pct"] - 0.5754) < 0.001
        assert abs(summary["avg_30d_pct"] - 0.5754) < 0.001  # all rows identical
        assert summary["delta_pp"] == pytest.approx(0.0, abs=0.01)
        assert summary["n_samples"] >= 1
        assert summary["latest_date"]  # non-empty


class TestBriefLineFormat:
    def test_format(self):
        # Use values whose *100 rounds cleanly so the test isn't fighting
        # IEEE-754 (0.575*100 → 57.4999... → "57", not "58").
        line = format_brief_line({
            "latest_date": "2026-05-13",
            "latest_pct":  0.58,
            "avg_30d_pct": 0.61,
            "n_samples":   25,
            "delta_pp":    -3.0,
        })
        assert "Short Vol: 58%" in line
        assert "30d avg 61%" in line
        assert "2026-05-13" in line

    def test_ticker_prefix_when_provided(self):
        """Given a ticker arg, When formatted, Then the line is prefixed
        so multi-ticker bursts stay unambiguous when stacked."""
        line = format_brief_line({
            "latest_date": "2026-05-13",
            "latest_pct":  0.58,
            "avg_30d_pct": 0.61,
            "n_samples":   25,
            "delta_pp":    -3.0,
        }, ticker="ebay")  # case-insensitive — uppercased on output
        assert line.startswith("EBAY Short Vol: 58%")


class TestCTOBurstExtractsShortVol:
    """The voice-layer CTO burst formatter should extract Short Vol and
    render an arrow indicating direction vs the 30d baseline."""

    def test_short_vol_above_baseline_shows_up_arrow(self):
        from agent_voice import _try_cto_burst
        content = (
            "GME DV Score: 65.4/100 ★★★★☆ (first score)\n"
            "Pillars — Valuation 9.6/25 · Capital 28.0/40 · Quality 12.8/20 · Insider 15.0/15\n"
            "Insider 3y buys: 21 purchases / $44.2M\n"
            "Immunity 4/5: ✗ Debt-free · ✓ Cash>$1B · ✓ Net Cash+ · ✓ Profitable · ✓ Altman Safe\n"
            "Inputs — EV/FCF 99.0\n"
            "Short Vol: 70% (30d avg 60%, as of 2026-05-13)"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert burst is not None
        assert "Short Vol: 70% ↑" in burst
        assert "(30d 60%)" in burst

    def test_short_vol_below_baseline_shows_down_arrow(self):
        from agent_voice import _try_cto_burst
        content = (
            "GME DV Score: 65.4/100 ★★★★☆ (first score)\n"
            "Short Vol: 50% (30d avg 60%, as of 2026-05-13)"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert "Short Vol: 50% ↓" in burst

    def test_short_vol_within_tolerance_shows_flat_arrow(self):
        """Within ±2pp of baseline → flat arrow (filter daily noise)."""
        from agent_voice import _try_cto_burst
        content = (
            "GME DV Score: 65.4/100 ★★★★☆ (first score)\n"
            "Short Vol: 61% (30d avg 60%, as of 2026-05-13)"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert "Short Vol: 61% →" in burst

    def test_burst_without_short_vol_omits_line(self):
        from agent_voice import _try_cto_burst
        content = (
            "GME DV Score: 65.4/100 ★★★★☆ (first score)\n"
            "Insider 3y buys: 21 purchases / $44.2M"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert "Short Vol" not in burst


class TestCTOBurstShowsTicker:
    """The CTO burst must surface the ticker so multi-ticker bursts
    stacked one after another (e.g. /dvburst GME EBAY) are unambiguous."""

    def test_headline_includes_ticker_when_prefixed_in_brief(self):
        from agent_voice import _try_cto_burst
        content = (
            "EBAY DV Score: 58.2/100 ★★★☆☆ (first score)\n"
            "Insider 3y buys: 3 purchases / $1.2M\n"
            "Inputs — Net Cash 12.0% · Altman Z 4.2"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert burst is not None
        assert "EBAY DV: 58.2/100" in burst

    def test_headline_defaults_to_gme_when_no_ticker_prefix(self):
        """Backward-compat: old briefs that didn't write the ticker into
        the content (pre-multi-ticker era) should still render as GME."""
        from agent_voice import _try_cto_burst
        content = (
            "DV Score: 65.4/100 ★★★★☆ (first score)\n"
            "Insider 3y buys: 21 purchases / $44.2M"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert burst is not None
        assert "GME DV: 65.4/100" in burst

    def test_short_vol_line_parses_with_ticker_prefix(self):
        """The short-vol regex must tolerate the new tickerised line
        format ('EBAY Short Vol: ...') without losing the arrow logic."""
        from agent_voice import _try_cto_burst
        content = (
            "EBAY DV Score: 58.0/100 ★★★☆☆ (first score)\n"
            "EBAY Short Vol: 45% (30d avg 60%, as of 2026-05-13)"
        )
        burst = _try_cto_burst(content, "2026-05-14T13:10:00-04:00")
        assert "EBAY DV: 58.0/100" in burst
        assert "Short Vol: 45% ↓" in burst
        assert "(30d 60%)" in burst
