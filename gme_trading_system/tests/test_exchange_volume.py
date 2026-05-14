"""Per-venue exchange-volume fetcher + aggregation + summary contract.

Verifies the TRF→DARK POOL aggregation, cache idempotency, soft-fail when
POLYGON_API_KEY is unset, the summary shape consumed by the CTO DV burst,
and the brief-line/full-table format.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_volume import (  # noqa: E402
    _aggregate,
    _venue_for,
    format_brief_line,
    format_full_table,
    get_exchange_volume_summary,
    update_for_ticker,
)


def _trade(exchange: int, size: int, price: float = 25.0) -> dict:
    """Build a minimal Polygon-shaped trade dict."""
    return {"exchange": exchange, "size": size, "price": price}


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def with_polygon_key(monkeypatch):
    """Ensure POLYGON_API_KEY is set so update_for_ticker doesn't short-circuit."""
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")


class TestAggregation:
    """The aggregator folds raw trades into per-venue rows. TRF carriers
    (Polygon exchange ids 4, 7, 21, 36) must collapse into a single
    "DARK POOL" bucket; lit venues stay separate."""

    def test_aggregates_trf_ids_into_dark_pool(self):
        """Given trades on exchange ids {4, 7, 36} (FINRA TRF carriers)
        and 11 (NYSE), When aggregated, Then DARK POOL sums the three TRF
        carriers and NYSE stays separate."""
        trades = [
            _trade(4, 1000),      # TRF NASDAQ Carteret → DARK POOL
            _trade(7, 2000),      # TRF NYSE Chicago → DARK POOL
            _trade(36, 500),      # legacy FINRA TRF → DARK POOL
            _trade(11, 300),      # NYSE → its own row
        ]
        agg = _aggregate(trades)
        assert "DARK POOL" in agg
        assert "NYSE" in agg
        dark_shares, _, dark_trades = agg["DARK POOL"]
        assert dark_shares == 1000 + 2000 + 500
        assert dark_trades == 3
        assert agg["NYSE"][0] == 300

    def test_unmapped_exchange_id_falls_through_to_raw_label(self):
        """An exchange id outside the known map (e.g. 22) should appear
        with a raw EXCH_<id> label rather than crashing or being merged
        into DARK POOL. Matches the redstripedtie convention of showing
        the raw venue code when no human name is known."""
        trades = [_trade(22, 100), _trade(11, 50)]
        agg = _aggregate(trades)
        assert "EXCH_22" in agg
        assert "DARK POOL" not in agg

    def test_skips_zero_size_and_zero_price_rows(self):
        """Defensive parsing: rows with size <= 0 or price <= 0 should be
        silently dropped so corrupt feed lines don't poison totals."""
        trades = [
            _trade(11, 0),         # zero size
            _trade(11, 100, 0.0),  # zero price
            _trade(11, 100, 25.0), # valid
        ]
        agg = _aggregate(trades)
        assert agg["NYSE"][0] == 100
        assert agg["NYSE"][2] == 1


class TestVenueMap:
    def test_known_trf_ids_resolve_to_dark_pool(self):
        for ex_id in (4, 7, 21, 36):
            assert _venue_for(ex_id) == "DARK POOL"

    def test_known_lit_ids_resolve_to_named_venue(self):
        assert _venue_for(11) == "NYSE"
        assert _venue_for(13) == "NASDAQ"
        assert _venue_for(6) == "IEX"

    def test_unknown_id_returns_raw_label(self):
        assert _venue_for(22) == "EXCH_22"


class TestUpdateForTicker:
    def test_soft_fails_when_api_key_unset(self, tmp_db, monkeypatch):
        """Given POLYGON_API_KEY is not in the env, When update_for_ticker
        runs, Then it returns 0 and never calls Polygon. Soft-fail keeps
        the rest of the CTO DV burst alive on fresh installs."""
        monkeypatch.delenv("POLYGON_API_KEY", raising=False)
        with patch("exchange_volume._iter_trades_for_day") as mock_iter:
            inserted = update_for_ticker("GME", days_back=5, db_path=tmp_db)
        assert inserted == 0
        mock_iter.assert_not_called()

    def test_cache_is_idempotent_on_same_day(self, tmp_db, with_polygon_key):
        """Given rows already cached for (date, ticker), When
        update_for_ticker runs again, Then the row count is unchanged and
        Polygon is not called for that day."""
        sample_trades = [_trade(4, 1000), _trade(11, 500)]

        # Use side_effect so each call yields a fresh iterator
        # (iter(list) is single-use — return_value would be exhausted after
        # the first call).
        def fresh_iter(*_a, **_kw):
            return iter(sample_trades)
        with patch("exchange_volume._iter_trades_for_day",
                   side_effect=fresh_iter):
            first = update_for_ticker("GME", days_back=3, db_path=tmp_db)
        assert first >= 1

        # Second run: fetcher should NOT be called (cache hit per day).
        call_count = [0]
        def counting_iter(*_a, **_kw):
            call_count[0] += 1
            return iter([])
        with patch("exchange_volume._iter_trades_for_day",
                   side_effect=counting_iter):
            second = update_for_ticker("GME", days_back=3, db_path=tmp_db)
        assert second == 0
        assert call_count[0] == 0


class TestGetSummary:
    def test_returns_none_when_no_data_and_polygon_silent(
        self, tmp_db, monkeypatch
    ):
        """Given an empty cache and Polygon returning no trades, When the
        summary is requested, Then None is returned (matches finra
        contract — caller decides whether to surface a message)."""
        monkeypatch.setenv("POLYGON_API_KEY", "test-key")
        with patch("exchange_volume._iter_trades_for_day", return_value=iter([])):
            summary = get_exchange_volume_summary("GME", db_path=tmp_db)
        assert summary is None

    def test_pct_of_real_includes_dark_pool_so_table_sums_to_100(
        self, tmp_db, with_polygon_key
    ):
        """Given a mix of DARK POOL and lit trades, When summarised, Then
        pct_of_real spans both buckets (sums ~100% across the venue list)
        AND total_real_shares reports the LIT-only total separately so
        downstream code can compute ratios cleanly. Mirrors the RST
        Exchange Volume table semantics."""
        trades = [
            _trade(4, 5577, 10.0),    # DARK POOL 55.77%
            _trade(11, 1079, 10.0),   # NYSE 10.79%
            _trade(13, 824, 10.0),    # NASDAQ 8.24%
            _trade(6, 596, 10.0),     # IEX 5.96%
            _trade(9, 632, 10.0),     # EDGX 6.32%
            _trade(12, 476, 10.0),    # NYSE ARCA 4.76%
            _trade(16, 349, 10.0),    # MEMX 3.49%
            _trade(15, 270, 10.0),    # BATS 2.70%
            _trade(8, 52, 10.0),      # EDGA 0.52%
            _trade(2, 28, 10.0),      # XBOS 0.28%
            _trade(3, 27, 10.0),      # XCIS 0.27%
            _trade(10, 6, 10.0),      # NYSE CHICAGO 0.06%
            _trade(1, 5, 10.0),       # NYSE AMEX 0.05%
            _trade(19, 4, 10.0),      # NASDAQ PHILLY 0.04%
            _trade(5, 70, 10.0),      # EPRL 0.70%
        ]
        with patch("exchange_volume._iter_trades_for_day", return_value=iter(trades)):
            summary = get_exchange_volume_summary("GME", db_path=tmp_db)

        assert summary is not None
        pct_total = sum(v["pct_of_real"] for v in summary["venues"])
        assert pct_total == pytest.approx(100.0, abs=0.01)
        # total_real_shares is LIT only — excludes DARK POOL.
        dark = next(v for v in summary["venues"] if v["venue"] == "DARK POOL")
        all_shares = sum(v["shares"] for v in summary["venues"])
        assert summary["total_real_shares"] == all_shares - dark["shares"]
        # dark_pool_pct is published separately and equals DARK POOL's
        # share of all_shares (including DARK POOL itself).
        assert summary["dark_pool_pct"] == pytest.approx(
            dark["shares"] / all_shares * 100.0, abs=0.01
        )


class TestFormatters:
    def test_format_brief_line_lists_top_three_with_dark_pool_first(self):
        """Given a 15-venue summary, When the brief line is rendered,
        Then DARK appears first (forced ordering) and the next two slots
        are the highest-share lit venues."""
        summary = {
            "date": "2026-05-13",
            "venues": [
                {"venue": "DARK POOL", "shares": 1153053, "notional_usd": 25e6,
                 "trades": 9280, "pct_of_real": 55.77},
                {"venue": "NYSE", "shares": 223089, "notional_usd": 4.9e6,
                 "trades": 1749, "pct_of_real": 10.79},
                {"venue": "NASDAQ", "shares": 170486, "notional_usd": 3.75e6,
                 "trades": 2577, "pct_of_real": 8.24},
                {"venue": "EDGX", "shares": 130796, "notional_usd": 2.87e6,
                 "trades": 1047, "pct_of_real": 6.32},
                # ...11 more elided
            ],
            "total_real_shares": 914453,
            "total_notional": 45e6,
            "total_trades": 19483,
            "dark_pool_pct": 55.77,
        }
        line = format_brief_line(summary)
        assert line.startswith("Venue Mix: DARK 55.8%")
        assert "NYSE 10.8%" in line
        assert "NDAQ 8.2%" in line
        assert "2026-05-13" in line
        assert "top-3 of 4" in line  # 4 venues in this fixture

    def test_brief_line_ticker_prefix_when_provided(self):
        """Given a ticker arg, When formatted, Then the line is prefixed
        ('GME Venue Mix: ...') so multi-ticker bursts stay unambiguous
        when stacked one after another."""
        summary = {
            "date": "2026-05-13",
            "venues": [
                {"venue": "DARK POOL", "shares": 100, "notional_usd": 1e3,
                 "trades": 10, "pct_of_real": 50.0},
                {"venue": "NYSE", "shares": 60, "notional_usd": 6e2,
                 "trades": 6, "pct_of_real": 30.0},
                {"venue": "NASDAQ", "shares": 40, "notional_usd": 4e2,
                 "trades": 4, "pct_of_real": 20.0},
            ],
            "total_real_shares": 100,
            "total_notional": 2e3,
            "total_trades": 20,
            "dark_pool_pct": 50.0,
        }
        line = format_brief_line(summary, ticker="ebay")
        assert line.startswith("EBAY Venue Mix:")

    def test_full_table_includes_every_venue_and_dark_pool_summary(self):
        summary = {
            "date": "2026-05-13",
            "venues": [
                {"venue": "DARK POOL", "shares": 1153053, "notional_usd": 25.37e6,
                 "trades": 9280, "pct_of_real": 55.77},
                {"venue": "NYSE", "shares": 223089, "notional_usd": 4.90e6,
                 "trades": 1749, "pct_of_real": 10.79},
            ],
            "total_real_shares": 223089,
            "total_notional": 30.27e6,
            "total_trades": 11029,
            "dark_pool_pct": 55.77,
        }
        table = format_full_table(summary)
        assert "DARK POOL" in table
        assert "NYSE" in table
        assert "2026-05-13" in table
        assert "Dark pool: 55.8%" in table
        assert "223,089" in table  # comma-formatted shares
