"""add exchange_volume table

Revision ID: 2026_05_14_exchange_volume
Revises: 2026_05_14_dv_rename
Create Date: 2026-05-14

Per-venue daily volume breakdown sourced from Polygon.io trades. Powers the
CTO DV burst's "Venue Mix" line and the /exchange Telegram command. "DARK
POOL" is the FINRA TRF/ADF aggregation (exchange ids 4, 7, 21, 36 in the
Polygon mapping).

Idempotent: IF NOT EXISTS on table and index. exchange_volume.py also
creates the schema lazily on first write, so fresh installs are covered
even if alembic has not run yet.
"""
from alembic import op


revision = "2026_05_14_exchange_volume"
down_revision = "2026_05_14_dv_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS exchange_volume ("
        "    date         TEXT NOT NULL,"
        "    ticker       TEXT NOT NULL,"
        "    venue        TEXT NOT NULL,"
        "    shares       INTEGER NOT NULL,"
        "    notional_usd REAL    NOT NULL,"
        "    trades       INTEGER NOT NULL,"
        "    fetched_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        "    PRIMARY KEY (date, ticker, venue)"
        ")"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_exvol_ticker_date "
        "ON exchange_volume(ticker, date)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_exvol_ticker_date")
    op.execute("DROP TABLE IF EXISTS exchange_volume")
