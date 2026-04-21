"""baseline_existing_schema

Applies the full schema from db_schema.sql for fresh databases.
For existing databases that already contain the schema, stamp instead:
    alembic stamp 4a2749c4b2f7

Revision ID: 4a2749c4b2f7
Revises:
Create Date: 2026-04-21 21:20:02.869676
"""
import os
from typing import Sequence, Union

from alembic import op


revision: str = "4a2749c4b2f7"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCHEMA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "db_schema.sql",
)


def upgrade() -> None:
    """Create all tables from db_schema.sql.

    Uses IF NOT EXISTS guards so this is safe on a partially-populated DB.
    """
    with open(SCHEMA_FILE, "r") as f:
        sql = f.read()

    # SQLite supports executescript but alembic's op.execute runs one statement.
    # Split on ';' and skip empties/comments.
    for stmt in sql.split(";"):
        stripped = stmt.strip()
        if stripped and not stripped.startswith("--"):
            op.execute(stripped)


def downgrade() -> None:
    """Drop all tables created by the baseline."""
    tables = [
        "options_snapshots", "social_posts", "short_watchlist",
        "structural_signals", "learning_sessions", "strategy_history",
        "performance_scores", "data_quality_logs", "stream_comments",
        "agent_logs", "trade_decisions", "predictions", "news_analysis",
        "trend_analysis", "daily_candles", "price_ticks",
    ]
    for table in tables:
        op.execute(f"DROP TABLE IF EXISTS {table}")
