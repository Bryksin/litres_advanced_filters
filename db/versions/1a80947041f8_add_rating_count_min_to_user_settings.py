"""add rating_count_min to user_settings

Revision ID: 1a80947041f8
Revises: 4fe736cc9c1c
Create Date: 2026-03-10 21:41:50.324747
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '1a80947041f8'
down_revision: Union[str, None] = '4fe736cc9c1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('rating_count_min', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('user_settings', 'rating_count_min')
