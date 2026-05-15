"""Add SEC FTD columns to market_fundamentals.

Revision ID: 2026_05_15_ftd
Revises: 2026_05_14_proj
Create Date: 2026-05-15

Surfaces fails-to-deliver intel on the OBS panel — latest fail quantity,
the settlement date it cleared on, and the rolling 30-calendar-day total.
Populated by fundamentals_feed.py via sec_ftd.get_ftd_summary on every
fundamentals refresh.

Idempotent: skips the ALTER if the column already exists (SQLite has no
IF NOT EXISTS for ADD COLUMN, so we check sqlite_master).
"""
from alembic import op


revision = "2026_05_15_ftd"
down_revision = "2026_05_14_proj"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, col: str) -> bool:
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_col(bind, "market_fundamentals", "ftd_latest_qty"):
        op.execute("ALTER TABLE market_fundamentals ADD COLUMN ftd_latest_qty INTEGER")
    if not _has_col(bind, "market_fundamentals", "ftd_latest_date"):
        op.execute("ALTER TABLE market_fundamentals ADD COLUMN ftd_latest_date TEXT")
    if not _has_col(bind, "market_fundamentals", "ftd_30d_qty"):
        op.execute("ALTER TABLE market_fundamentals ADD COLUMN ftd_30d_qty INTEGER")


def downgrade() -> None:
    # SQLite doesn't support ALTER TABLE DROP COLUMN before 3.35.
    # Leaving the columns in place is harmless — they're nullable.
    pass
