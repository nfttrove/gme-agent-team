"""Add market_fundamentals table for OBS stats panel.

Revision ID: 2026_05_14_fundamentals
Revises: 2026_05_14_dv_rename
Create Date: 2026-05-14

Stores a daily snapshot of GME fundamentals (market cap, revenue TTM,
net income TTM, EPS, shares out, PE, beta, 52-week range, prev close,
next earnings date) plus YoY % deltas for the income-statement items.

Source: yfinance Ticker.info + .quarterly_financials. Refreshed daily
at 08:35 ET by orchestrator.run_fundamentals_update().

Consumed by the local OBS dashboard panel at /obs/stats (logger_daemon.py).
"""
from alembic import op


revision = "2026_05_14_fundamentals"
down_revision = "2026_05_14_dv_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS market_fundamentals (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp              TEXT    NOT NULL,
            market_cap             REAL,
            market_cap_yoy_pct     REAL,
            revenue_ttm            REAL,
            revenue_yoy_pct        REAL,
            net_income_ttm         REAL,
            net_income_yoy_pct     REAL,
            eps_ttm                REAL,
            eps_yoy_pct            REAL,
            shares_out             REAL,
            shares_out_yoy_pct     REAL,
            pe_ratio               REAL,
            beta                   REAL,
            fifty_two_week_low     REAL,
            fifty_two_week_high    REAL,
            prev_close             REAL,
            next_earnings_date     TEXT,
            dark_pool_pct          REAL,
            dark_pool_volume       INTEGER,
            dark_pool_date         TEXT
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_fundamentals_ts "
        "ON market_fundamentals(timestamp DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_market_fundamentals_ts")
    op.execute("DROP TABLE IF EXISTS market_fundamentals")
