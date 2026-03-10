"""add junction table indexes for query performance

Revision ID: 4fe736cc9c1c
Revises: 0001
Create Date: 2026-03-10 16:40:01.740634
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '4fe736cc9c1c'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_book_author_person_id', 'book_author', ['person_id'], unique=False)
    op.create_index('ix_book_genre_genre_id', 'book_genre', ['genre_id'], unique=False)
    op.create_index('ix_book_narrator_person_id', 'book_narrator', ['person_id'], unique=False)
    op.create_index('ix_book_series_series_id', 'book_series', ['series_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_book_series_series_id', table_name='book_series')
    op.drop_index('ix_book_narrator_person_id', table_name='book_narrator')
    op.drop_index('ix_book_genre_genre_id', table_name='book_genre')
    op.drop_index('ix_book_author_person_id', table_name='book_author')
