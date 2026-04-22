"""Add signal confidence and feedback loop tables.

Revision ID: 2026_04_22_signal_confidence
Revises: 4a2749c4b2f7_baseline_existing_schema
Create Date: 2026-04-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2026_04_22_signal_confidence'
down_revision = '4a2749c4b2f7_baseline_existing_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create signal_alerts and signal_feedback tables for confidence tracking."""

    # Create signal_alerts table — each alert fired to Telegram
    op.create_table(
        'signal_alerts',
        sa.Column('id', sa.String(36), primary_key=True),  # UUID
        sa.Column('agent_name', sa.String(50), nullable=False),
        sa.Column('signal_type', sa.String(50), nullable=False),  # 'price_prediction', 'pattern', 'sentiment', etc.
        sa.Column('confidence', sa.Float(), nullable=False),  # 0.0-1.0 (or 0-100)
        sa.Column('severity', sa.String(20), nullable=True),  # 'HIGH', 'MEDIUM', 'LOW'
        sa.Column('entry_price', sa.Float(), nullable=True),
        sa.Column('stop_loss', sa.Float(), nullable=True),
        sa.Column('take_profit', sa.Float(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('telegram_message_id', sa.Integer(), nullable=True),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('idx_signal_alerts_agent', 'signal_alerts', ['agent_name'])
    op.create_index('idx_signal_alerts_timestamp', 'signal_alerts', ['timestamp'])
    op.create_index('idx_signal_alerts_type', 'signal_alerts', ['signal_type'])

    # Create signal_feedback table — team response to each alert
    op.create_table(
        'signal_feedback',
        sa.Column('id', sa.String(36), primary_key=True),  # UUID
        sa.Column('alert_id', sa.String(36), sa.ForeignKey('signal_alerts.id'), nullable=False),
        sa.Column('action_taken', sa.String(20), nullable=False),  # 'executed', 'ignored', 'missed'
        sa.Column('execution_timestamp', sa.DateTime(), nullable=True),
        sa.Column('entry_price', sa.Float(), nullable=True),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('quantity', sa.Float(), nullable=True),
        sa.Column('pnl', sa.Float(), nullable=True),  # Realized P&L in dollars
        sa.Column('pnl_pct', sa.Float(), nullable=True),  # P&L as percentage
        sa.Column('team_member', sa.String(100), nullable=True),
        sa.Column('team_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('idx_signal_feedback_alert_id', 'signal_feedback', ['alert_id'])
    op.create_index('idx_signal_feedback_action', 'signal_feedback', ['action_taken'])

    # Add confidence + signal_type columns to agent_logs if not already there
    # (optional: just for easier querying)
    try:
        op.add_column('agent_logs', sa.Column('confidence', sa.Float(), nullable=True))
    except Exception:
        pass  # Column may already exist

    try:
        op.add_column('agent_logs', sa.Column('signal_type', sa.String(50), nullable=True))
    except Exception:
        pass


def downgrade() -> None:
    """Drop signal feedback tables."""
    op.drop_index('idx_signal_feedback_action')
    op.drop_index('idx_signal_feedback_alert_id')
    op.drop_table('signal_feedback')

    op.drop_index('idx_signal_alerts_type')
    op.drop_index('idx_signal_alerts_timestamp')
    op.drop_index('idx_signal_alerts_agent')
    op.drop_table('signal_alerts')

    try:
        op.drop_column('agent_logs', 'signal_type')
    except Exception:
        pass

    try:
        op.drop_column('agent_logs', 'confidence')
    except Exception:
        pass
