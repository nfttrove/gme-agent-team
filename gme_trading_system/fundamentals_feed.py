"""
GME fundamentals snapshot for the OBS dashboard panel.

Pulls market cap, revenue TTM, net income TTM, EPS, shares outstanding, PE,
beta, 52-week range, previous close, and next earnings date from yfinance.
Computes YoY % deltas for income-statement items (revenue, net income, EPS,
market cap, shares out) by summing trailing 4 quarters vs the prior 4 quarters.

Persisted to the `market_fundamentals` table (timestamped). Served via
logger_daemon's /obs/stats.json endpoint.

Usage:
    from fundamentals_feed import FundamentalsFeed
    snap = FundamentalsFeed().snapshot()        # dict of fields
    FundamentalsFeed().update_db()              # persist one row

Refreshed daily at 08:35 ET by orchestrator.run_fundamentals_update().
"""
import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL  = "GME"
YT_HANDLE = os.getenv("OBS_YT_HANDLE", "TroveIsland")


class FundamentalsFeed:

    def __init__(self):
        self._ticker = yf.Ticker(SYMBOL)

    def snapshot(self) -> dict:
        """
        Return a flat dict of fundamentals + YoY deltas.

        YoY % deltas are computed from the two most recent annual rows of
        `income_stmt` / `balance_sheet` (stockanalysis.com convention). The
        market-cap YoY is reconstructed from price history × shares-on-file
        a year ago.

        Any field yfinance can't supply comes back as None.
        Never raises; logs and returns partial.
        """
        info = self._safe_info()
        income = self._safe_stmt("income_stmt")
        balance = self._safe_stmt("balance_sheet")

        market_cap     = info.get("marketCap")
        revenue_ttm    = self._latest(income, "Total Revenue") or info.get("totalRevenue")
        ni_ttm         = self._latest(income, "Net Income") or info.get("netIncomeToCommon")
        eps_ttm        = self._latest(income, "Diluted EPS") or info.get("trailingEps")
        shares_out     = info.get("sharesOutstanding") or self._latest(balance, "Ordinary Shares Number")

        shares_prev_yr = self._prior(balance, "Ordinary Shares Number")
        market_cap_prev = self._market_cap_one_year_ago(shares_prev_yr or shares_out)

        return {
            "market_cap":           market_cap,
            "market_cap_yoy_pct":   self._yoy_pct(market_cap, market_cap_prev),
            "revenue_ttm":          revenue_ttm,
            "revenue_yoy_pct":      self._yoy_pct(revenue_ttm, self._prior(income, "Total Revenue")),
            "net_income_ttm":       ni_ttm,
            "net_income_yoy_pct":   self._yoy_pct(ni_ttm,      self._prior(income, "Net Income")),
            "eps_ttm":              eps_ttm,
            "eps_yoy_pct":          self._yoy_pct(eps_ttm,     self._prior(income, "Diluted EPS")),
            "shares_out":           shares_out,
            "shares_out_yoy_pct":   self._yoy_pct(shares_out,  shares_prev_yr),
            "pe_ratio":             info.get("trailingPE"),
            "beta":                 info.get("beta"),
            "fifty_two_week_low":   info.get("fiftyTwoWeekLow"),
            "fifty_two_week_high":  info.get("fiftyTwoWeekHigh"),
            "prev_close":           info.get("regularMarketPreviousClose") or info.get("previousClose"),
            **self._earnings(info),
            **self._dark_pool(),
            **self._youtube(),
        }

    def _earnings(self, info: dict) -> dict:
        e = self._next_earnings_info(info)
        return {
            "next_earnings_date":      e["date"],
            "next_earnings_projected": e["projected"],
        }

    @staticmethod
    def _youtube() -> dict:
        """Latest subscriber count for the configured channel handle."""
        try:
            from youtube_feed import get_subscriber_count
            return {
                "yt_handle":      YT_HANDLE,
                "yt_subscribers": get_subscriber_count(YT_HANDLE),
            }
        except Exception as e:
            log.debug(f"[fundamentals] youtube fetch failed: {e}")
            return {"yt_handle": YT_HANDLE, "yt_subscribers": None}

    @staticmethod
    def _dark_pool() -> dict:
        """FINRA short-volume proxy for dark pool participation.

        Uses finra_short_vol (the canonical FINRA wiring — circuit breaker,
        30-day cached backfill) rather than the inline scraper in
        options_feed.py which only goes back 10 days uncached.
        """
        try:
            from finra_short_vol import get_short_vol_summary, DB_PATH as FINRA_DB
            summary = get_short_vol_summary("GME")
            if not summary:
                return {"dark_pool_pct": None, "dark_pool_volume": None, "dark_pool_date": None}
            conn = sqlite3.connect(FINRA_DB)
            row = conn.execute(
                "SELECT short_volume FROM finra_short_vol "
                "WHERE ticker='GME' AND date=? LIMIT 1",
                (summary["latest_date"],),
            ).fetchone()
            conn.close()
            return {
                "dark_pool_pct":    round(summary["latest_pct"] * 100, 2),
                "dark_pool_volume": int(row[0]) if row else None,
                "dark_pool_date":   summary["latest_date"],
            }
        except Exception as e:
            log.debug(f"[fundamentals] dark pool fetch failed: {e}")
            return {"dark_pool_pct": None, "dark_pool_volume": None, "dark_pool_date": None}

    def update_db(self) -> bool:
        snap = self.snapshot()
        if not any(v is not None for v in snap.values()):
            log.warning("[fundamentals] All fields None — yfinance likely rate-limited. Skipping write.")
            return False

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO market_fundamentals
               (timestamp, market_cap, market_cap_yoy_pct,
                revenue_ttm, revenue_yoy_pct,
                net_income_ttm, net_income_yoy_pct,
                eps_ttm, eps_yoy_pct,
                shares_out, shares_out_yoy_pct,
                pe_ratio, beta,
                fifty_two_week_low, fifty_two_week_high,
                prev_close,
                next_earnings_date, next_earnings_projected,
                dark_pool_pct, dark_pool_volume, dark_pool_date,
                yt_handle, yt_subscribers)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(ET).isoformat(),
                snap["market_cap"], snap["market_cap_yoy_pct"],
                snap["revenue_ttm"], snap["revenue_yoy_pct"],
                snap["net_income_ttm"], snap["net_income_yoy_pct"],
                snap["eps_ttm"], snap["eps_yoy_pct"],
                snap["shares_out"], snap["shares_out_yoy_pct"],
                snap["pe_ratio"], snap["beta"],
                snap["fifty_two_week_low"], snap["fifty_two_week_high"],
                snap["prev_close"],
                snap["next_earnings_date"], int(bool(snap["next_earnings_projected"])),
                snap["dark_pool_pct"], snap["dark_pool_volume"], snap["dark_pool_date"],
                snap["yt_handle"], snap["yt_subscribers"],
            ),
        )
        conn.commit()
        conn.close()
        log.info(f"[fundamentals] Wrote snapshot: mcap={snap['market_cap']} pe={snap['pe_ratio']} earnings={snap['next_earnings_date']}")
        return True

    def _safe_info(self) -> dict:
        try:
            return self._ticker.info or {}
        except Exception as e:
            log.error(f"[fundamentals] Ticker.info failed: {e}")
            return {}

    def _safe_stmt(self, name: str):
        """Pull an annual statement (income_stmt or balance_sheet). yfinance
        returns columns newest-to-oldest so iloc[0] is the latest fiscal year."""
        try:
            return getattr(self._ticker, name)
        except Exception as e:
            log.error(f"[fundamentals] {name} failed: {e}")
            return None

    @staticmethod
    def _latest(stmt, row: str):
        return FundamentalsFeed._col_at(stmt, row, 0)

    @staticmethod
    def _prior(stmt, row: str):
        return FundamentalsFeed._col_at(stmt, row, 1)

    @staticmethod
    def _col_at(stmt, row: str, idx: int):
        if stmt is None:
            return None
        try:
            if row not in stmt.index or idx >= stmt.shape[1]:
                return None
            val = stmt.loc[row].iloc[idx]
            if val is None:
                return None
            f = float(val)
            return None if f != f else f  # NaN check
        except Exception:
            return None

    def _market_cap_one_year_ago(self, shares_prev_yr):
        """Approximate market cap one year ago = close ~252 trading days back × shares-on-file then."""
        if shares_prev_yr is None:
            return None
        try:
            h = self._ticker.history(period="2y", interval="1d")
            if h is None or h.empty or len(h) < 252:
                return None
            return float(h["Close"].iloc[-252]) * float(shares_prev_yr)
        except Exception as e:
            log.debug(f"[fundamentals] history fetch for mcap YoY failed: {e}")
            return None

    @staticmethod
    def _yoy_pct(current, prior):
        if current is None or prior is None or prior == 0:
            return None
        try:
            return round((float(current) - float(prior)) / abs(float(prior)) * 100, 1)
        except Exception:
            return None

    def _next_earnings_info(self, info: dict) -> dict:
        """Return {'date': ISO YYYY-MM-DD, 'projected': bool}.

        Source priority:
          1. Ticker.calendar["Earnings Date"] — the authoritative next-date
             that powers stockanalysis.com, Yahoo's quote page, etc.
          2. Ticker.earnings_dates DataFrame — first future row.
          3. info["earningsTimestamp"] — often stale (lags the prior report).
        If all three are in the past, project ~one fiscal quarter forward
        from the most recent known date and flag projected=True.
        """
        from datetime import date as _date, timedelta
        today = datetime.now(ET).date()

        # 1. Ticker.calendar
        try:
            cal = self._ticker.calendar or {}
            ed = cal.get("Earnings Date")
            if ed:
                d = ed[0] if isinstance(ed, list) else ed
                if isinstance(d, _date) and d >= today:
                    return {"date": d.isoformat(), "projected": False}
        except Exception as e:
            log.debug(f"[fundamentals] Ticker.calendar failed: {e}")

        # 2. earnings_dates DataFrame
        most_recent_past = None
        try:
            ed_df = self._ticker.earnings_dates
            if ed_df is not None and not ed_df.empty:
                import pandas as pd
                idx = ed_df.index.tz_localize(None) if ed_df.index.tz is not None else ed_df.index
                future = idx[idx >= pd.Timestamp(today)]
                if len(future):
                    return {"date": future.min().date().isoformat(), "projected": False}
                most_recent_past = idx.max().date()
        except Exception as e:
            log.debug(f"[fundamentals] earnings_dates failed: {e}")

        # 3. info["earningsTimestamp"]
        ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if ts:
            try:
                d = datetime.fromtimestamp(int(ts), tz=ET).date()
                if d >= today:
                    return {"date": d.isoformat(), "projected": False}
                if most_recent_past is None or d > most_recent_past:
                    most_recent_past = d
            except Exception:
                pass

        # 4. Projection fallback. GME reports ~91 days apart on a stable
        #    cadence (early Jun / Sep / Dec / late Mar). Roll forward from
        #    the most recent past date until we land in the future.
        if most_recent_past is None:
            return {"date": None, "projected": False}
        projected = most_recent_past + timedelta(days=91)
        while projected < today:
            projected += timedelta(days=91)
        return {"date": projected.isoformat(), "projected": True}


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    feed = FundamentalsFeed()
    print(json.dumps(feed.snapshot(), indent=2, default=str))
