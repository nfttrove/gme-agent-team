"""Add YouTube subscriber columns to market_fundamentals.

Revision ID: 2026_05_14_youtube
Revises: 8d52de3c3015
Create Date: 2026-05-14

Surfaces the channel handle + live subscriber count next to the
SUBSCRIBE CTA on the OBS panel. Populated by youtube_feed.py on every
fundamentals refresh.

Idempotent: skips the ALTER if the column already exists (SQLite has
no IF NOT EXISTS for ADD COLUMN, so we check sqlite_master).
"""
from alembic import op


revision = "2026_05_14_youtube"
down_revision = "8d52de3c3015"
branch_labels = None
depends_on = None


def _has_col(bind, table: str, col: str) -> bool:
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_col(bind, "market_fundamentals", "yt_handle"):
        op.execute("ALTER TABLE market_fundamentals ADD COLUMN yt_handle TEXT")
    if not _has_col(bind, "market_fundamentals", "yt_subscribers"):
        op.execute("ALTER TABLE market_fundamentals ADD COLUMN yt_subscribers INTEGER")


def downgrade() -> None:
    # SQLite doesn't support ALTER TABLE DROP COLUMN before 3.35.
    # Leaving the columns in place is harmless — they're nullable.
    pass
