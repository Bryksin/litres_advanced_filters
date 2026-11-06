"""add sort to user_settings

Revision ID: be02feffd618
Revises: 6b499b328700
Create Date: 2026-11-06 18:34:42.028866
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'be02feffd618'
down_revision: Union[str, None] = '6b499b328700'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('sort', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('user_settings', 'sort')
