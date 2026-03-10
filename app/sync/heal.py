"""Heal command — fix structural gaps in book data.

Detects its own gaps via SQL queries: no book IDs needed from the user.
Supports: heal missing narrators (BUG-1), heal missing genres (BUG-2).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import BookGenre, BookNarrator, Person
from app.sync.common import ensure_genre
from app.scrapers.arts import fetch_arts_detail
from app.scrapers.client import RateLimitedClient
from app.sync.ingest import NARRATOR_ROLES

log = logging.getLogger(__name__)


def _find_books_without_narrators(session: Session) -> list[int]:
    """Return book IDs that have zero book_narrator rows."""
    rows = session.execute(
        text(
            "SELECT b.id FROM book b "
            "LEFT JOIN book_narrator bn ON bn.book_id = b.id "
            "WHERE bn.book_id IS NULL "
            "ORDER BY b.id"
        )
    ).fetchall()
    return [r[0] for r in rows]


def _find_books_without_genres(session: Session) -> list[int]:
    """Return book IDs that have zero book_genre rows."""
    rows = session.execute(
        text(
            "SELECT b.id FROM book b "
            "LEFT JOIN book_genre bg ON bg.book_id = b.id "
            "WHERE bg.book_id IS NULL "
            "ORDER BY b.id"
        )
    ).fetchall()
    return [r[0] for r in rows]


def heal_narrators(
    session: Session,
    client: RateLimitedClient,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Re-fetch arts detail for narrator-less books and backfill BookNarrator rows.

    Returns dict with counts: total, healed, unresolvable, failed.
    """
    book_ids = _find_books_without_narrators(session)
    stats = {"total": len(book_ids), "healed": 0, "unresolvable": 0, "failed": 0}

    if not book_ids:
        log.info("heal_narrators: no books without narrators found")
        return stats

    log.info("heal_narrators: %d books without narrators", len(book_ids))

    for book_id in book_ids:
        try:
            detail = fetch_arts_detail(client, book_id)
            narrator_persons = list(
                {p.id: p for p in detail.art.persons if p.role in NARRATOR_ROLES}.values()
            )

            if not narrator_persons:
                stats["unresolvable"] += 1
                log.debug("heal_narrators: book %d — no narrator in arts detail", book_id)
                continue

            if dry_run:
                stats["healed"] += 1
                log.info("heal_narrators: [dry-run] book %d — would add %d narrator(s)", book_id, len(narrator_persons))
                continue

            for p in narrator_persons:
                if session.get(Person, p.id) is None:
                    session.add(Person(id=p.id, full_name=p.full_name, url=p.url or ""))
                    session.flush()

                if session.get(BookNarrator, {"book_id": book_id, "person_id": p.id}) is None:
                    session.add(BookNarrator(book_id=book_id, person_id=p.id))

            session.flush()
            stats["healed"] += 1
            log.info(
                "heal_narrators: book %d — added %d narrator(s): %s",
                book_id,
                len(narrator_persons),
                ", ".join(p.full_name for p in narrator_persons),
            )

        except Exception:
            stats["failed"] += 1
            log.exception("heal_narrators: book %d — fetch failed", book_id)

    log.info(
        "heal_narrators done: %d total, %d healed, %d unresolvable, %d failed",
        stats["total"], stats["healed"], stats["unresolvable"], stats["failed"],
    )
    return stats


def heal_genres(
    session: Session,
    client: RateLimitedClient,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Re-fetch arts detail for genre-less books and backfill BookGenre rows.

    Returns dict with counts: total, healed, still_empty, failed.
    """
    book_ids = _find_books_without_genres(session)
    stats = {"total": len(book_ids), "healed": 0, "still_empty": 0, "failed": 0}

    if not book_ids:
        log.info("heal_genres: no books without genres found")
        return stats

    log.info("heal_genres: %d books without genres", len(book_ids))
    now = datetime.now(timezone.utc)

    for book_id in book_ids:
        try:
            detail = fetch_arts_detail(client, book_id)
            added = 0

            for genre_ref in detail.genres:
                genre_id = ensure_genre(session, genre_ref)
                if genre_id is None:
                    log.debug("heal_genres: book %d genre_id=%s has no name — skipping", book_id, genre_ref.id)
                    continue

                if session.get(BookGenre, {"book_id": book_id, "genre_id": genre_id}) is None:
                    if not dry_run:
                        session.add(BookGenre(book_id=book_id, genre_id=genre_id, cached_at=now))
                    added += 1

            if added > 0:
                if not dry_run:
                    session.flush()
                stats["healed"] += 1
                log.info("heal_genres: book %d — %s %d genre link(s)",
                         book_id, "would add" if dry_run else "added", added)
            else:
                stats["still_empty"] += 1
                log.debug("heal_genres: book %d — no matching genres in table", book_id)

        except Exception:
            stats["failed"] += 1
            log.exception("heal_genres: book %d — fetch failed", book_id)

    log.info(
        "heal_genres done: %d total, %d healed, %d still empty, %d failed",
        stats["total"], stats["healed"], stats["still_empty"], stats["failed"],
    )
    return stats
