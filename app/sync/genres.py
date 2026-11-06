"""Fetch LitRes genre tree and upsert into the genre table.

Run before first bulk sync: BookGenre rows reference genre.id via FK.
If genre table is empty, all BookGenre inserts will fail with FK violation.

Usage: python -m app.sync genres [--verbose]
"""

import logging
import time

from app.config.config import Config
from app.db import SessionLocal
from sqlalchemy import delete, select

from app.db.models import Genre, GenreParent
from app.scrapers.client import RateLimitedClient
from app.scrapers.genres import fetch_genre_tree_hierarchical
from app.scrapers.models import GenreNode
from app.sync.logging_setup import setup_sync_logging

log = logging.getLogger(__name__)


def upsert_genre_tree(session, roots: list[GenreNode]) -> int:
    """Upsert every node of the scraped genre DAG and its parent edges.

    A genre can appear under several parents (S2). Each appearance adds a
    ``genre_parent`` edge; ``genre.parent_id`` keeps the first parent seen in
    this walk (or the existing one if the genre was not re-attached). Stale
    edges are removed only below parents whose child list was scraped
    completely, so a failed sub-genre page never wipes edges.

    Returns the number of distinct genre rows upserted.
    """
    seen_nodes: set[str] = set()
    ordered_edges: list[tuple[str, str]] = []  # walk order: shallow edges first wins
    complete_parents: set[str] = set()

    def walk(node: GenreNode, parent_id: str | None, path: frozenset[str]) -> None:
        if node.id in path:  # defensive: never follow a cycle
            return
        if parent_id is not None:
            ordered_edges.append((node.id, parent_id))
        if node.children_complete and node.children:
            complete_parents.add(node.id)

        if node.id not in seen_nodes:
            seen_nodes.add(node.id)
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

    # Breadth-first so level-1/2 edges are added before the deeper ones that
    # could close a cycle with them.
    queue: list[tuple[GenreNode, str | None, frozenset[str]]] = [(r, None, frozenset()) for r in roots]
    while queue:
        node, parent_id, path = queue.pop(0)
        walk(node, parent_id, path)
        if node.id not in path:
            queue.extend((c, node.id, path | {node.id}) for c in node.children)

    existing_edges = {
        (g, p) for g, p in session.execute(select(GenreParent.genre_id, GenreParent.parent_id))
    }
    scraped = set(ordered_edges)
    # Prune edges that disappeared below fully scraped parents
    stale = [
        (g, p) for g, p in existing_edges
        if p in complete_parents and (g, p) not in scraped
    ]
    kept = existing_edges - set(stale)

    # Accept scraped edges in order, dropping any that would close a cycle
    # (the sidebar renders genre.children recursively).
    parents_of: dict[str, set[str]] = {}
    for g, p in kept:
        parents_of.setdefault(g, set()).add(p)

    def _is_ancestor(candidate: str, of: str) -> bool:
        seen, stack = set(), [of]
        while stack:
            cur = stack.pop()
            if cur == candidate:
                return True
            for p in parents_of.get(cur, ()):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return False

    edges: set[tuple[str, str]] = set()
    cycles = 0
    for g, p in dict.fromkeys(ordered_edges):
        if (g, p) in kept:
            edges.add((g, p))
            continue
        if g == p or _is_ancestor(g, p):
            cycles += 1
            log.warning("Genre tree: dropping edge %s -> parent %s (would create a cycle)", g, p)
            continue
        parents_of.setdefault(g, set()).add(p)
        edges.add((g, p))
        session.add(GenreParent(genre_id=g, parent_id=p))
    for genre_id, parent_id in stale:
        session.execute(
            delete(GenreParent).where(
                GenreParent.genre_id == genre_id, GenreParent.parent_id == parent_id
            )
        )
    session.flush()

    multi = sum(1 for g in seen_nodes if sum(1 for e in edges if e[0] == g) > 1)
    log.info(
        "Genre tree: %d genres, %d edges (%d new, %d removed, %d cyclic dropped), "
        "%d genres with several parents",
        len(seen_nodes), len(edges), len(edges - existing_edges), len(stale), cycles, multi,
    )
    return len(seen_nodes)


def sync_genre_tree(session, client) -> int:
    """Fetch the LitRes genre tree and upsert it (no logging setup, no commit)."""
    roots = fetch_genre_tree_hierarchical(client)
    if not roots:
        raise RuntimeError("Genre tree page parsed to 0 root genres — layout changed?")
    return upsert_genre_tree(session, roots)


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
        total = upsert_genre_tree(session, roots)
        session.commit()

    elapsed = time.monotonic() - start
    log.info("Genres done: %d rows upserted in %.1fs", total, elapsed)
    print(f"Genres: {total} rows upserted in {elapsed:.1f}s")
