"""rename trove_score_history → dv_score_history

Revision ID: 2026_05_14_dv_rename
Revises: 2026_04_22_signal_confidence
Create Date: 2026-05-14

Renames the table and indexes used by the deep-value scoring history.
Trove was an internal nickname; DV (Deep Value) is the canonical name.

SQLite has no ALTER INDEX RENAME, so indexes are dropped and recreated.
ALTER TABLE RENAME preserves rows + WAL safely.

Idempotent: skips both rename and index recreation if the old table is
absent (fresh install) or the new table already exists. The renamed
dv_history.py creates the new table lazily for installs that never
held the old name.
"""
from alembic import op


revision = "2026_05_14_dv_rename"
down_revision = "2026_04_22_signal_confidence"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    row = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()

    has_old = _table_exists(bind, "trove_score_history")
    has_new = _table_exists(bind, "dv_score_history")

    if has_old and not has_new:
        op.execute("ALTER TABLE trove_score_history RENAME TO dv_score_history")

    # Drop the old-named indexes (they survive table rename in SQLite).
    op.execute("DROP INDEX IF EXISTS idx_trove_hist_date")
    op.execute("DROP INDEX IF EXISTS idx_trove_hist_ticker")
    op.execute("DROP INDEX IF EXISTS idx_trove_hist_unresolved")

    # Create new-named indexes only when the table exists. On a truly fresh
    # install neither table is present yet — dv_history.py's _ensure_schema
    # will create the table + indexes on first write.
    if has_old or has_new:
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_dv_hist_date "
            "ON dv_score_history(score_date)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_dv_hist_ticker "
            "ON dv_score_history(ticker)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_dv_hist_unresolved "
            "ON dv_score_history(score_date) "
            "WHERE return_30d IS NULL OR return_90d IS NULL OR return_365d IS NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()

    has_new = _table_exists(bind, "dv_score_history")
    has_old = _table_exists(bind, "trove_score_history")

    if has_new and not has_old:
        op.execute("ALTER TABLE dv_score_history RENAME TO trove_score_history")

    op.execute("DROP INDEX IF EXISTS idx_dv_hist_date")
    op.execute("DROP INDEX IF EXISTS idx_dv_hist_ticker")
    op.execute("DROP INDEX IF EXISTS idx_dv_hist_unresolved")

    if has_old or has_new:
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_trove_hist_date "
            "ON trove_score_history(score_date)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_trove_hist_ticker "
            "ON trove_score_history(ticker)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_trove_hist_unresolved "
            "ON trove_score_history(score_date) "
            "WHERE return_30d IS NULL OR return_90d IS NULL OR return_365d IS NULL"
        )
