"""Core ingest functions: ingest_book(), ingest_series(), helpers.

These are called by run_bulk(). They are stateless (no global
state) and receive all dependencies as arguments for testability.
"""

import logging
from dataclasses import dataclass
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

# Arts calls one newly discovered series may spend during the crawl. Larger
# rosters (e.g. a 693-book radio archive) are finished by the nightly roster
# refresh, which has its own budget; already linked members cost nothing.
NEW_SERIES_MEMBER_CALLS = 200


@dataclass
class MemberBudget:
    """Arts calls left for roster members; shared across several series."""

    remaining: int


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


def refresh_known_book(book: Book, art: Art) -> None:
    """Refresh the fields of a known book from a fresh Art sighting (S3).

    Called for every crawl sighting and every arts-detail fetch, so the
    subscription flags follow LitRes in both directions. ``cached_at`` is the
    "last refreshed from LitRes" timestamp the stale-book refresh orders by.
    Rating storage is unchanged (rating_count=0 means unrated).
    """
    book.rating_avg = art.rating.rated_avg
    book.rating_count = art.rating.rated_total_count
    book.is_available_with_subscription = art.is_available_with_subscription
    book.is_abonement_art = art.is_abonement_art
    if art.title:
        book.title = art.title
    if art.cover_url:
        book.cover_url = art.cover_url
    book.cached_at = datetime.now(timezone.utc)


def tag_root_genre(session: Session, book_id: int, genre_id: str) -> None:
    """Link a crawled book to the crawl's root genre (S2).

    Every book of the bulk crawl is in the root genre by construction, even
    when LitRes only tags it with genres that sit outside the local subtree.
    Caller must make sure the genre row exists (FK).
    """
    if session.get(BookGenre, {"book_id": book_id, "genre_id": genre_id}) is None:
        session.add(BookGenre(book_id=book_id, genre_id=genre_id, cached_at=datetime.now(timezone.utc)))


def _series_art_order(detail: ArtDetail, series_id: int) -> int | None:
    for s_ref in detail.series:
        if s_ref.id == series_id:
            return s_ref.art_order
    return None


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
    - Known book: refreshes the catalog-card fields (rating, subscription
      flags, title, cover, cached_at) — see refresh_known_book(). No arts call.
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
            refresh_known_book(existing, art)
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
    trigger: tuple[int, int | None] | None = None,
    budget: MemberBudget | None = None,
    commit_each: bool = False,
) -> int:
    """Upsert the Series row and read its full roster.

    ``trigger`` = (book_id, art_order) of the book that revealed the series;
    it is linked right after the Series row is written, so the roster walk
    does not spend an arts call on it.

    ``budget`` caps the arts calls spent on members (None = unlimited). When
    it runs out the roster stays unsynced and the next refresh continues
    where this one stopped (linked members are skipped).

    ``commit_each`` commits the session after every member (only for callers
    that are not inside a savepoint, i.e. the nightly refresh) to keep SQLite
    write-lock windows short.

    - The Series row is always written first, so the caller can link the
      triggering book even when the roster cannot be read (S1).
    - A roster fetch failure (WAF 403, 5xx, timeout) is logged and is NOT
      raised: ``roster_synced_at`` stays NULL and the nightly roster refresh
      retries it.
    - An empty series URL (the arts API returns some series without one) is
      skipped: fetching it would request the LitRes home page.
    - Members that are already linked cost nothing. Other members get one
      arts call each: new books are ingested, known books refreshed, and the
      link uses LitRes ``art_order`` for this series (S6), falling back to
      the roster page order. Each member runs in its own savepoint.
    - ``roster_synced_at`` is set only when every member succeeded.

    Returns the number of members linked by this call.
    """
    if dry_run:
        return 0

    # Network first: the roster is fetched before the first write, so the
    # SQLite write lock is not held across these requests.
    series_page = None
    fetch_error: Exception | None = None
    if series_url:
        try:
            series_page = fetch_series_page(client, series_url, art_types="audiobook", languages="ru")
        except Exception as exc:
            fetch_error = exc

    series = session.get(Series, series_id)
    slug = series_url.rstrip("/").rsplit("/", 1)[-1] if series_url else None
    if series is None:
        series = Series(
            id=series_id,
            name=series_name,
            slug=slug,
            url=series_url,
            book_count=book_count,
        )
        session.add(series)
    else:
        if series_name:
            series.name = series_name
        if book_count:
            series.book_count = book_count
        if series_url and series.url != series_url:
            series.url = series_url
            series.slug = slug
    session.flush()
    if trigger is not None:
        _upsert_book_series_link(session, trigger[0], series_id, trigger[1])

    if not series_url:
        log.info("ingest_series: series_id=%d '%s' has no URL — roster skipped", series_id, series_name)
        return 0
    if fetch_error is not None:
        log.warning(
            "ingest_series: roster fetch failed for series_id=%d url=%s (%s) — "
            "series kept, roster retried by the nightly refresh",
            series_id, series_url, fetch_error,
        )
        return 0
    if not series_page:
        log.warning("ingest_series: failed to parse series page for id=%d url=%s", series_id, series_url)
        return 0

    linked = 0
    failed = 0
    out_of_budget = False
    for entry in series_page.books:
        if session.get(BookSeries, {"book_id": entry.art_id, "series_id": series_id}) is not None:
            continue
        if budget is not None:
            if budget.remaining <= 0:
                out_of_budget = True
                break
            budget.remaining -= 1
        sp = session.begin_nested()
        try:
            detail = fetch_arts_detail(client, entry.art_id)
            ingest_book(session, client, detail.art, arts_detail=detail)
            position = _series_art_order(detail, series_id)
            _upsert_book_series_link(
                session, entry.art_id, series_id, position if position is not None else entry.position
            )
            sp.commit()
            if commit_each:
                session.commit()
            linked += 1
        except Exception:
            if sp.is_active:
                sp.rollback()
            else:
                session.rollback()
            failed += 1
            log.exception("ingest_series: failed to ingest member art_id=%d (series_id=%d)", entry.art_id, series_id)

    if failed == 0 and not out_of_budget:
        series.roster_synced_at = datetime.now(timezone.utc)
    session.flush()
    log.info(
        "ingest_series: series_id=%d '%s' — roster %d, %d newly linked, %d failed%s",
        series_id, series_name, len(series_page.books), linked, failed,
        " (member budget exhausted — continues next refresh)" if out_of_budget else "",
    )
    return linked


def link_book_series(
    session: Session,
    client: RateLimitedClient,
    book_id: int,
    detail: ArtDetail,
) -> tuple[int, int]:
    """Create or refresh the series links of one book from its arts detail.

    Shared by run_bulk (new books) and retry-failed (S5). For each real series
    (``art_order`` set; author collections have none):
      - a new series is created and its roster read (ingest_series);
      - a known series gets its ``book_count`` refreshed (S11) and, if this
        book was not linked yet, its roster is queued for re-reading
        (``roster_synced_at = NULL``), because a new volume appeared;
      - the book is linked with its LitRes ``art_order``.
    Each series runs in its own savepoint: a series failure never removes
    the book (S1).

    Returns (new_series_count, roster_members_linked).
    """
    new_series = 0
    members = 0
    for s_ref in detail.series:
        if s_ref.art_order is None:
            continue  # author collection, not a series
        sp = session.begin_nested()
        try:
            series = session.get(Series, s_ref.id)
            if series is None:
                log.info(
                    "[book %d] new series '%s' (id=%d, %d books) — ingesting",
                    book_id, s_ref.name, s_ref.id, s_ref.unique_arts_count,
                )
                members += ingest_series(
                    session, client,
                    s_ref.id, s_ref.name, s_ref.url, s_ref.unique_arts_count,
                    trigger=(book_id, s_ref.art_order),
                    budget=MemberBudget(NEW_SERIES_MEMBER_CALLS),
                )
                new_series += 1
            else:
                if s_ref.unique_arts_count:
                    series.book_count = s_ref.unique_arts_count
                if s_ref.url and not series.url:
                    series.url = s_ref.url
                    series.slug = s_ref.url.rstrip("/").rsplit("/", 1)[-1]
                if session.get(BookSeries, {"book_id": book_id, "series_id": s_ref.id}) is None:
                    series.roster_synced_at = None
            _upsert_book_series_link(session, book_id, s_ref.id, s_ref.art_order)
            sp.commit()
        except Exception:
            sp.rollback()
            log.exception("[book %d] series id=%d '%s' — link failed, book kept", book_id, s_ref.id, s_ref.name)
    return new_series, members
