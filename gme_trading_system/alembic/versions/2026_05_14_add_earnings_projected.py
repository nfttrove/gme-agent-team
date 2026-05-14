"""Add next_earnings_projected flag to market_fundamentals.

Revision ID: 2026_05_14_proj
Revises: 2026_05_14_youtube
Create Date: 2026-05-14

When yfinance's next earnings date is in the past (its calendar lags
the report by a few weeks), fundamentals_feed projects the next
quarterly date forward by ~91 days and sets this flag. The OBS panel
swaps "expected on" → "projected for" so viewers know the date is an
estimate, not a confirmed announcement.

Idempotent: skips the ALTER if the column already exists.
"""
from alembic import op


revision = "2026_05_14_proj"
down_revision = "2026_05_14_youtube"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, col: str) -> bool:
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_col(bind, "market_fundamentals", "next_earnings_projected"):
        op.execute(
            "ALTER TABLE market_fundamentals "
            "ADD COLUMN next_earnings_projected INTEGER DEFAULT 0"
        )


def downgrade() -> None:
    pass  # SQLite DROP COLUMN unsupported; column is harmless if left.
