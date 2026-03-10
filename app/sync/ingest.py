"""Core ingest functions: ingest_book(), ingest_series(), helpers.

These are called by run_bulk(). They are stateless (no global
state) and receive all dependencies as arguments for testability.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import (
    Book,
    BookAuthor,
    BookGenre,
    BookNarrator,
    BookSeries,
    Person,
    Series,
)
from app.scrapers.arts import ArtDetail, fetch_arts_detail
from app.scrapers.client import RateLimitedClient
from app.scrapers.models import Art, PersonRef
from app.scrapers.series import fetch_series_page
from app.sync.common import ensure_genre

log = logging.getLogger(__name__)

# LitRes uses "reader" for human narrators and "narrator" for AI auto-readers.
NARRATOR_ROLES = frozenset(("reader", "narrator"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_person(session: Session, p: PersonRef) -> None:
    """Insert Person if not already in DB. Null-safe: url=None → empty string."""
    if session.get(Person, p.id) is None:
        session.add(
            Person(
                id=p.id,
                full_name=p.full_name,
                url=p.url or "",
            )
        )
        session.flush()


def _upsert_book_series_link(
    session: Session, book_id: int, series_id: int, position: int | None
) -> None:
    """Insert or update BookSeries junction row."""
    existing = session.get(BookSeries, {"book_id": book_id, "series_id": series_id})
    if existing is None:
        session.add(BookSeries(book_id=book_id, series_id=series_id, position_in_series=position))
    else:
        existing.position_in_series = position
    session.flush()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_book(
    session: Session,
    client: RateLimitedClient,
    art: Art,
    arts_detail: ArtDetail | None = None,
    *,
    dry_run: bool = False,
) -> tuple[bool, ArtDetail | None]:
    """Ingest one book from a catalog Art object.

    - New book: fetches arts detail (unless pre-supplied), upserts Book + all junctions.
    - Known book: updates rating_avg + rating_count only (no arts detail call).
    - dry_run=True: detects new/known but writes nothing to DB.

    Returns (is_new, arts_detail):
      - is_new: True if book was new, False if known (rating updated)
      - arts_detail: the ArtDetail used (None if known or dry_run)

    Caller uses returned arts_detail for series handling (avoids double fetch).
    NOTE: Does NOT handle series links — caller (run_bulk) handles series.
    """
    existing = session.get(Book, art.id)

    if existing is not None:
        if not dry_run:
            existing.rating_avg = art.rating.rated_avg
            existing.rating_count = art.rating.rated_total_count
        return False, None

    if dry_run:
        return True, None

    # New book — fetch arts detail if not supplied
    if arts_detail is None:
        arts_detail = fetch_arts_detail(client, art.id)

    now = datetime.now(timezone.utc)

    # Upsert Book
    book = Book(
        id=art.id,
        title=art.title,
        url=art.url,
        art_type=art.art_type,
        cover_url=art.cover_url,
        language_code=art.language_code,
        rating_avg=art.rating.rated_avg,
        rating_count=art.rating.rated_total_count,
        is_available_with_subscription=art.is_available_with_subscription,
        is_abonement_art=art.is_abonement_art,
        release_date=arts_detail.release_date,
        cached_at=now,
    )
    session.add(book)
    session.flush()

    # BookGenre — auto-insert unknown genres, skip nameless ones
    for genre_ref in arts_detail.genres:
        genre_id = ensure_genre(session, genre_ref)
        if genre_id is None:
            log.debug("[book %d] genre_id=%s has no name — skipping", art.id, genre_ref.id)
            continue
        if session.get(BookGenre, {"book_id": art.id, "genre_id": genre_id}) is None:
            session.add(BookGenre(book_id=art.id, genre_id=genre_id, cached_at=now))

    # Persons from catalog card (authors + narrators)
    # Dedup by person_id: LitRes can return same person with multiple roles
    # (e.g. "narrator" + "reader" for AI auto-readers). dict keeps first occurrence.
    authors = list({p.id: p for p in art.persons if p.role == "author"}.values())
    narrators = list({p.id: p for p in art.persons if p.role in NARRATOR_ROLES}.values())

    for i, p in enumerate(authors):
        _upsert_person(session, p)
        if session.get(BookAuthor, {"book_id": art.id, "person_id": p.id}) is None:
            session.add(BookAuthor(book_id=art.id, person_id=p.id, sort_order=i))

    for p in narrators:
        _upsert_person(session, p)
        if session.get(BookNarrator, {"book_id": art.id, "person_id": p.id}) is None:
            session.add(BookNarrator(book_id=art.id, person_id=p.id))

    session.flush()
    return True, arts_detail


def ingest_series(
    session: Session,
    client: RateLimitedClient,
    series_id: int,
    series_name: str,
    series_url: str,
    book_count: int,
    *,
    dry_run: bool = False,
) -> int:
    """Fetch series page and ingest all members.

    Creates/updates the Series row, fetches series page HTML, and calls
    ingest_book() for each member (which calls arts detail for new books).
    Returns number of member books ingested (new + updated).
    """
    if dry_run:
        return 0

    # Upsert Series row
    existing_series = session.get(Series, series_id)
    slug = series_url.rstrip("/").rsplit("/", 1)[-1] if series_url else None
    if existing_series is None:
        session.add(Series(
            id=series_id,
            name=series_name,
            slug=slug,
            url=series_url,
            book_count=book_count,
        ))
    else:
        existing_series.name = series_name
        existing_series.book_count = book_count
    session.flush()

    # Fetch series page (audiobook, ru, no subscription filter)
    series_page = fetch_series_page(client, series_url, art_types="audiobook", languages="ru")
    if not series_page:
        log.warning("ingest_series: failed to parse series page for id=%d url=%s", series_id, series_url)
        return 0

    ingested = 0
    for entry in series_page.books:
        try:
            # Fetch arts detail for this series member to get persons/genres/release_date
            detail = fetch_arts_detail(client, entry.art_id)
            ingest_book(session, client, detail.art, arts_detail=detail)  # tuple result unused here
            _upsert_book_series_link(session, entry.art_id, series_id, entry.position)
            ingested += 1
        except Exception:
            log.exception("ingest_series: failed to ingest member art_id=%d", entry.art_id)

    session.flush()
    log.info("ingest_series: series_id=%d '%s' — %d/%d members ingested",
             series_id, series_name, ingested, len(series_page.books))
    return ingested
