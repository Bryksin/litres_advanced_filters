"""genre_parent edges, series roster_synced_at, sync_run heartbeat_at

Revision ID: a2a6ea3fd938
Revises: be02feffd618
Create Date: 2026-11-06 18:37:17.812057

- genre_parent: many-to-many genre -> parent edges (LitRes genres form a DAG).
  Backfilled from the existing genre.parent_id so the tree is unchanged until
  the next genre sync adds the extra parents.
- series.roster_synced_at: NULL for every existing series, so the nightly
  bounded roster refresh re-reads every roster once (they were read at most
  one page deep before this fix).
- sync_run.heartbeat_at: liveness for the zombie reaper.

Safe on a populated DB: one new table, two nullable columns, one INSERT.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.base import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'a2a6ea3fd938'
down_revision: Union[str, None] = 'be02feffd618'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'genre_parent',
        sa.Column('genre_id', sa.String(length=32), nullable=False),
        sa.Column('parent_id', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['genre_id'], ['genre.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_id'], ['genre.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('genre_id', 'parent_id'),
    )
    op.create_index('ix_genre_parent_parent_id', 'genre_parent', ['parent_id'], unique=False)
    op.execute(
        "INSERT INTO genre_parent (genre_id, parent_id) "
        "SELECT g.id, g.parent_id FROM genre g "
        "WHERE g.parent_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM genre p WHERE p.id = g.parent_id)"
    )
    with op.batch_alter_table('series') as batch:
        batch.add_column(sa.Column('roster_synced_at', UTCDateTime(), nullable=True))
    with op.batch_alter_table('sync_run') as batch:
        batch.add_column(sa.Column('heartbeat_at', UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sync_run') as batch:
        batch.drop_column('heartbeat_at')
    with op.batch_alter_table('series') as batch:
        batch.drop_column('roster_synced_at')
    op.drop_index('ix_genre_parent_parent_id', table_name='genre_parent')
    op.drop_table('genre_parent')
