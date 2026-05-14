"""merge fundamentals + exchange_volume heads

Revision ID: 8d52de3c3015
Revises: 2026_05_14_fundamentals, 2026_05_14_exchange_volume
Create Date: 2026-05-14 18:05:46.790929

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d52de3c3015'
down_revision: Union[str, Sequence[str], None] = ('2026_05_14_fundamentals', '2026_05_14_exchange_volume')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
