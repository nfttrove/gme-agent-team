"""SEC FTD fetcher + cache + summary contract.

Verifies zip parsing, file-level cache dedup, soft-fail on 404 / bad-zip,
and the summary shape consumed by the CTO DV burst.
"""
from __future__ import annotations

import io
import sqlite3
import sys
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sec_ftd import (  # noqa: E402
    _fetch_one_file,
    _iter_recent_file_ids,
    _parse_zip_for_ticker,
    format_brief_line,
    get_ftd_summary,
    update_for_ticker,
)


_SAMPLE_TEXT = (
    "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
    "20260408|36467W109|GME|62913|GAMESTOP CORP (HLDG CO) CL A|23.43\n"
    "20260409|36467W109|GME|48242|GAMESTOP CORP (HLDG CO) CL A|22.91\n"
    "20260410|36467W109|GME|47202|GAMESTOP CORP (HLDG CO) CL A|22.87\n"
    "20260411|037833100|AAPL|1000|APPLE INC COM|180.0\n"
    "Trailer record count 4\n"
    "Trailer total quantity of shares 159357\n"
)


def _make_zip(text: str, inner_name: str = "cnsfails202604a") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, text)
    return buf.getvalue()


_SAMPLE_ZIP = _make_zip(_SAMPLE_TEXT)


class _OKBytes:
    """Fake requests.Response carrying zip bytes."""

    status_code = 200

    def __init__(self, content: bytes):
        self.content = content


class _NotFound:
    status_code = 404
    content = b"Not Found"


class TestParser:
    def test_parses_known_ticker(self):
        rows = _parse_zip_for_ticker(_SAMPLE_ZIP, "GME")
        # Three GME rows, ordered as written in the source file.
        assert len(rows) == 3
        iso, qty, price, cusip, desc = rows[0]
        assert iso == "2026-04-08"
        assert qty == 62913
        assert price == 23.43
        assert cusip == "36467W109"
        assert "GAMESTOP" in desc

    def test_returns_empty_for_missing_ticker(self):
        assert _parse_zip_for_ticker(_SAMPLE_ZIP, "ZZZZ") == []

    def test_case_insensitive(self):
        rows = _parse_zip_for_ticker(_SAMPLE_ZIP, "gme")
        assert len(rows) == 3

    def test_skips_trailer_rows(self):
        """The two 'Trailer ...' lines at the end of the source file must
        not be parsed as data rows."""
        rows = _parse_zip_for_ticker(_SAMPLE_ZIP, "GME")
        for iso, *_ in rows:
            assert iso.startswith("2026-")  # not a "Trailer..." artifact

    def test_malformed_date_skipped(self):
        bad = (
            "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
            "not-a-date|36467W109|GME|100|GME|10.0\n"
            "20260408|36467W109|GME|200|GME|11.0\n"
        )
        rows = _parse_zip_for_ticker(_make_zip(bad), "GME")
        assert len(rows) == 1
        assert rows[0][0] == "2026-04-08"

    def test_bad_zip_returns_empty(self):
        """Non-zip bytes should soft-fail to an empty list, not raise."""
        assert _parse_zip_for_ticker(b"not a zip", "GME") == []


class TestFetcher:
    def test_404_returns_none(self):
        with patch("sec_ftd.requests.get", return_value=_NotFound()):
            assert _fetch_one_file("202604b") is None

    def test_non_zip_content_rejected(self):
        """A 200 OK with HTML content (e.g. SEC error page) should soft-fail
        rather than crash the zip parser downstream."""
        with patch("sec_ftd.requests.get", return_value=_OKBytes(b"<html>oops</html>")):
            assert _fetch_one_file("202604a") is None

    def test_well_formed_returns_bytes(self):
        with patch("sec_ftd.requests.get", return_value=_OKBytes(_SAMPLE_ZIP)):
            content = _fetch_one_file("202604a")
            assert content is not None
            assert content[:4].startswith(b"PK\x03\x04")


class TestFileIdIterator:
    def test_newest_first(self):
        ids = _iter_recent_file_ids(months_back=2)
        # months_back=2 yields 3 months × 2 halves = 6 IDs
        assert len(ids) == 6
        # First entry is current-month 'b', last entry is oldest-month 'a'
        assert ids[0].endswith("b")
        assert ids[-1].endswith("a")

    def test_handles_year_rollover(self):
        with patch("sec_ftd.date") as mock_date:
            mock_date.today.return_value = date(2026, 1, 15)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            ids = _iter_recent_file_ids(months_back=2)
        # Jan 2026 → backfill should reach Nov 2025 without month=0
        assert "202511a" in ids
        assert "202511b" in ids
        # And no malformed month-zero strings
        for fid in ids:
            assert "00" not in fid[:6]  # month digits never "00"


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


class TestUpdateForTicker:
    def test_inserts_rows_when_fresh(self, tmp_db):
        with patch("sec_ftd._fetch_one_file", return_value=_SAMPLE_ZIP):
            inserted = update_for_ticker("GME", months_back=1, db_path=tmp_db)
        # Three GME rows per file × however many file_ids the iterator
        # yields. Idempotency on (settlement_date, ticker) PRIMARY KEY
        # means duplicate file payloads collapse to 3 distinct rows.
        assert inserted == 3
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute(
            "SELECT COUNT(*) FROM sec_ftd WHERE ticker='GME'"
        ).fetchone()
        conn.close()
        assert rows[0] == 3

    def test_idempotent_second_run_does_not_refetch(self, tmp_db):
        """Files marked done in sec_ftd_files for THIS ticker should not be refetched."""
        with patch("sec_ftd._fetch_one_file", return_value=_SAMPLE_ZIP):
            update_for_ticker("GME", months_back=1, db_path=tmp_db)

        call_count = [0]

        def counting_fetch(_file_id):
            call_count[0] += 1
            return _SAMPLE_ZIP

        with patch("sec_ftd._fetch_one_file", side_effect=counting_fetch):
            second = update_for_ticker("GME", months_back=1, db_path=tmp_db)
        assert second == 0
        assert call_count[0] == 0

    def test_different_ticker_not_starved_by_prior_ticker(self, tmp_db):
        """Per-ticker dedup: when GME has marked files done, fetching
        AAPL should still refetch (the file contains every ticker; we
        just hadn't parsed it for AAPL yet). Regression test for the
        bug where file-level (not (file, ticker)-level) dedup starved
        subsequent tickers."""
        # Process GME first — marks files done for GME
        with patch("sec_ftd._fetch_one_file", return_value=_SAMPLE_ZIP):
            update_for_ticker("GME", months_back=1, db_path=tmp_db)

        # Now process AAPL — must refetch and find its row
        call_count = [0]

        def counting_fetch(_file_id):
            call_count[0] += 1
            return _SAMPLE_ZIP

        with patch("sec_ftd._fetch_one_file", side_effect=counting_fetch):
            inserted = update_for_ticker("AAPL", months_back=1, db_path=tmp_db)
        # Sample has 1 AAPL row
        assert inserted == 1
        assert call_count[0] > 0  # actually re-fetched the file for AAPL

    def test_migrates_legacy_sec_ftd_files_schema(self, tmp_db):
        """A DB created by the first version of this module had
        sec_ftd_files keyed by file_id alone (no ticker column). The
        migration in _ensure_schema must drop and recreate so the new
        per-ticker dedup INSERT/SELECT works without crashing."""
        # Seed the legacy schema by hand
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "CREATE TABLE sec_ftd_files ("
            "  file_id TEXT PRIMARY KEY, "
            "  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.execute("INSERT INTO sec_ftd_files (file_id) VALUES ('202604a')")
        conn.commit()
        conn.close()

        # Now run a normal update — should silently migrate and succeed
        with patch("sec_ftd._fetch_one_file", return_value=_SAMPLE_ZIP):
            inserted = update_for_ticker("GME", months_back=1, db_path=tmp_db)
        assert inserted == 3  # 3 GME rows from sample

        # Verify schema is now the new shape
        conn = sqlite3.connect(tmp_db)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(sec_ftd_files)").fetchall()}
        conn.close()
        assert "ticker" in cols, "migration did not add ticker column"

    def test_failed_fetch_not_marked_done(self, tmp_db):
        """A None response (404 / outage) must NOT mark the file as
        processed, so the next cron retries it."""
        with patch("sec_ftd._fetch_one_file", return_value=None):
            update_for_ticker("GME", months_back=1, db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        files = conn.execute("SELECT COUNT(*) FROM sec_ftd_files").fetchone()
        conn.close()
        assert files[0] == 0


class TestSummary:
    def test_returns_none_when_no_data(self, tmp_db):
        with patch("sec_ftd._fetch_one_file", return_value=None):
            summary = get_ftd_summary("GME", db_path=tmp_db)
        assert summary is None

    def test_summary_shape(self, tmp_db):
        with patch("sec_ftd._fetch_one_file", return_value=_SAMPLE_ZIP):
            summary = get_ftd_summary("GME", db_path=tmp_db)
        assert summary is not None
        # ORDER BY settlement_date DESC → 2026-04-10 is latest in sample
        assert summary["latest_date"] == "2026-04-10"
        assert summary["latest_qty"] == 47202
        # All 3 sample rows are within 30 calendar days of latest (Apr 8-10)
        assert summary["rolling_30d_qty"] == 62913 + 48242 + 47202
        assert summary["n_samples"] == 3
        assert summary["latest_price"] == 22.87

    def test_summary_window_excludes_rows_older_than_30d(self, tmp_db):
        """Rows older than 30 calendar days from the latest settlement
        must NOT be included in rolling_30d_qty. Regression test for
        the prior 'last 14 rows' semantics."""
        # Manually seed: one recent row + one >30 days older
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(tmp_db)
        from sec_ftd import _ensure_schema
        _ensure_schema(conn)
        conn.executescript(
            "INSERT INTO sec_ftd (settlement_date, ticker, fails_quantity) "
            "VALUES ('2026-04-10', 'GME', 100);"
            "INSERT INTO sec_ftd (settlement_date, ticker, fails_quantity) "
            "VALUES ('2026-02-01', 'GME', 999);"
        )
        conn.commit()
        conn.close()
        with patch("sec_ftd._fetch_one_file", return_value=None):
            # _fetch returns None → no new rows added; we read seeded data
            summary = get_ftd_summary("GME", db_path=tmp_db)
        assert summary is not None
        assert summary["latest_date"] == "2026-04-10"
        # Only the recent row counts; the Feb row is >30 days older
        assert summary["rolling_30d_qty"] == 100
        assert summary["n_samples"] == 1


class TestBriefLineFormat:
    def test_format_small_qty(self):
        line = format_brief_line({
            "latest_date":     "2026-04-13",
            "latest_qty":      600,
            "rolling_30d_qty": 420_239,
            "n_samples":       8,
            "latest_price":    23.22,
        })
        assert "FTDs: 600" in line
        assert "settled 2026-04-13" in line
        assert "30d total 420.2K" in line

    def test_format_large_qty_uses_k_suffix(self):
        line = format_brief_line({
            "latest_date":     "2026-04-08",
            "latest_qty":      62_913,
            "rolling_30d_qty": 159_357,
            "n_samples":       3,
            "latest_price":    23.43,
        })
        assert "FTDs: 62.9K" in line
        assert "30d total 159.4K" in line

    def test_format_million_uses_m_suffix(self):
        line = format_brief_line({
            "latest_date":     "2026-04-08",
            "latest_qty":      1_500_000,
            "rolling_30d_qty": 12_000_000,
            "n_samples":       14,
            "latest_price":    23.43,
        })
        assert "1.50M" in line
        assert "12.00M" in line

    def test_ticker_prefix_when_provided(self):
        line = format_brief_line({
            "latest_date":     "2026-04-13",
            "latest_qty":      600,
            "rolling_30d_qty": 420_000,
            "n_samples":       8,
            "latest_price":    23.22,
        }, ticker="gme")  # case-insensitive — uppercased on output
        assert line.startswith("GME FTDs:")
