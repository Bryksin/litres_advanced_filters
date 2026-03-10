"""Fetch LitRes genre tree and upsert into the genre table.

Run before first bulk sync: BookGenre rows reference genre.id via FK.
If genre table is empty, all BookGenre inserts will fail with FK violation.

Usage: python -m app.sync genres [--verbose]
"""

import logging
import time

from app.config.config import Config
from app.db import SessionLocal
from app.db.models import Genre
from app.scrapers.client import RateLimitedClient
from app.scrapers.genres import fetch_genre_tree_hierarchical
from app.scrapers.models import GenreNode
from app.sync.logging_setup import setup_sync_logging

log = logging.getLogger(__name__)


def _upsert_node(session, node: GenreNode, parent_id: str | None) -> int:
    """Upsert one genre node and all its descendants. Returns count of upserted rows."""
    existing = session.get(Genre, node.id)
    if existing is None:
        session.add(
            Genre(
                id=node.id,
                parent_id=parent_id,
                name=node.name,
                slug=node.slug,
                url=node.url,
                count=node.count,
            )
        )
    else:
        existing.name = node.name
        existing.count = node.count
        existing.parent_id = parent_id
    session.flush()

    count = 1
    for child in node.children:
        count += _upsert_node(session, child, parent_id=node.id)
    return count


def run_genres(*, verbose: bool = False) -> None:
    """Fetch full hierarchical genre tree from LitRes and upsert all nodes into genre table."""
    from app.sync.common import make_run_tag
    setup_sync_logging(Config.SYNC_DIR, make_run_tag(), verbose=verbose)

    start = time.monotonic()
    log.info("Fetching genre tree from LitRes...")
    with RateLimitedClient() as client:
        roots = fetch_genre_tree_hierarchical(client)

    log.info("Upserting %d root genres into DB...", len(roots))
    with SessionLocal() as session:
        total = 0
        for root in roots:
            total += _upsert_node(session, root, parent_id=None)
        session.commit()

    elapsed = time.monotonic() - start
    log.info("Genres done: %d rows upserted in %.1fs", total, elapsed)
    print(f"Genres: {total} rows upserted in {elapsed:.1f}s")
