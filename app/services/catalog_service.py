"""CatalogService — filtered, sorted, paginated catalog queries.

All data comes from the sync-populated DB. Zero HTTP calls to LitRes.

Two-query approach:
1. SQL GROUP BY with LIMIT/OFFSET to get paginated card groups + total count
2. Load detail data only for the page's cards
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, literal
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Book,
    BookAuthor,
    BookGenre,
    BookNarrator,
    BookSeries,
    Person,
    Series,
    UserIgnoredBook,
    UserListenedBook,
)
from app.models.catalog_query import CatalogQuery
from app.services.cards import BookCard, Card, SeriesCard
from app.services.genre_service import get_genre_descendant_ids

log = logging.getLogger(__name__)

_LITRES_BASE = "https://www.litres.ru"


def _abs_url(path: str) -> str:
    if path.startswith("http"):
        return path
    return _LITRES_BASE + path


def _sorted_author_names(book_authors: list[BookAuthor]) -> list[str]:
    return [ba.person.full_name for ba in sorted(book_authors, key=lambda x: x.sort_order)]


def _first_narrator_name(book_narrators: list[BookNarrator]) -> str | None:
    if not book_narrators:
        return None
    return book_narrators[0].person.full_name


@dataclass
class CatalogResult:
    """Result of get_catalog(): cards + pagination metadata."""
    cards: list[Card]
    total_count: int
    page: int
    page_size: int
    has_next: bool


def _build_filtered_book_query(
    session: Session,
    query: CatalogQuery,
    user_id: int,
):
    """Build a query on Book with all WHERE filters applied, but no eager loading or .all().

    Returns a SQLAlchemy query object filtered on Book.
    """
    q = session.query(Book)

    # Audiobook only
    q = q.filter(Book.art_type == 1)

    # Genre (includes all descendant genres)
    if query.genre_id:
        genre_ids = get_genre_descendant_ids(session, query.genre_id)
        q = q.filter(
            Book.id.in_(
                session.query(BookGenre.book_id).filter(BookGenre.genre_id.in_(genre_ids))
            )
        )

    # Series only / Standalones only
    has_series = session.query(BookSeries.book_id).filter(BookSeries.book_id == Book.id).exists()
    if query.series_only:
        q = q.filter(has_series)
    elif query.standalones_only:
        q = q.filter(~has_series)

    # Series size filter
    if query.series_only and (query.series_min is not None or query.series_max is not None):
        series_size_sub = (
            session.query(BookSeries.book_id)
            .join(Series, Series.id == BookSeries.series_id)
        )
        if query.series_min is not None:
            series_size_sub = series_size_sub.filter(Series.book_count >= query.series_min)
        if query.series_max is not None:
            series_size_sub = series_size_sub.filter(Series.book_count <= query.series_max)
        q = q.filter(Book.id.in_(series_size_sub))

    # Full series under subscription (F-10)
    if query.series_only and query.full_series_subscription:
        series_with_non_sub = (
            session.query(BookSeries.series_id)
            .join(Book, Book.id == BookSeries.book_id)
            .filter(Book.is_available_with_subscription == False)  # noqa: E712
            .subquery()
        )
        q = q.filter(
            ~Book.id.in_(
                session.query(BookSeries.book_id)
                .filter(BookSeries.series_id.in_(session.query(series_with_non_sub.c.series_id)))
            )
        )

    # Author exclusion
    if query.exclude_authors and query.excluded_authors:
        excluded_author_books = (
            session.query(BookAuthor.book_id)
            .join(Person, Person.id == BookAuthor.person_id)
            .filter(Person.full_name.in_(query.excluded_authors))
        )
        q = q.filter(~Book.id.in_(excluded_author_books))

    # Narrator exclusion (F-6)
    if query.exclude_narrators and query.excluded_narrators:
        excluded_narrator_books = (
            session.query(BookNarrator.book_id)
            .join(Person, Person.id == BookNarrator.person_id)
            .filter(Person.full_name.in_(query.excluded_narrators))
        )
        q = q.filter(~Book.id.in_(excluded_narrator_books))

    # Rating range (F-8)
    if query.rating_min is not None:
        q = q.filter(Book.rating_avg >= query.rating_min)
    if query.rating_max is not None:
        q = q.filter(Book.rating_avg <= query.rating_max)

    # Ignore list (F-9)
    ignored_books = (
        session.query(UserIgnoredBook.book_id)
        .filter(UserIgnoredBook.user_id == user_id)
    )
    q = q.filter(~Book.id.in_(ignored_books))

    # Hide listened (F-5) — only when NOT using F-11
    if query.hide_listened and not query.incomplete_series_only:
        listened_books = (
            session.query(UserListenedBook.book_id)
            .filter(UserListenedBook.user_id == user_id)
        )
        q = q.filter(~Book.id.in_(listened_books))

    # Incomplete series only (F-11) — pre-filter
    if query.incomplete_series_only:
        listened_series_ids = (
            session.query(BookSeries.series_id)
            .join(UserListenedBook, UserListenedBook.book_id == BookSeries.book_id)
            .filter(UserListenedBook.user_id == user_id)
        )
        q = q.filter(
            Book.id.in_(
                session.query(BookSeries.book_id)
                .filter(BookSeries.series_id.in_(listened_series_ids))
            )
        )

    return q


def _get_paginated_card_groups(session, base_q, sort_key, page, page_size):
    """SQL-level grouping and pagination.

    Groups filtered books by series_id (for series books) or book.id (for standalones).
    Returns (page_groups, total_count) where page_groups is a list of rows with:
        group_key, card_type, max_release_date, avg_rating, total_rating_count, book_count
    """
    filtered_ids = base_q.with_entities(Book.id).subquery().select()

    # Group key: series_id for series books, book.id for standalones
    group_key = func.coalesce(BookSeries.series_id, Book.id).label("group_key")
    card_type = case(
        (BookSeries.series_id.isnot(None), literal("series")),
        else_=literal("book"),
    ).label("card_type")

    groups_q = (
        session.query(
            group_key,
            card_type,
            func.max(Book.release_date).label("max_release_date"),
            func.avg(Book.rating_avg).label("avg_rating"),
            func.sum(func.coalesce(Book.rating_count, 0)).label("total_rating_count"),
            func.count().label("book_count"),
        )
        .select_from(Book)
        .outerjoin(BookSeries, BookSeries.book_id == Book.id)
        .filter(Book.id.in_(filtered_ids))
        .group_by(group_key, card_type)
    )

    # Apply sort
    if sort_key == "rating_avg_desc":
        groups_q = groups_q.order_by(
            func.round(func.avg(Book.rating_avg), 1).desc(),
            func.sum(func.coalesce(Book.rating_count, 0)).desc(),
        )
    elif sort_key == "rating_count_desc":
        groups_q = groups_q.order_by(
            func.sum(func.coalesce(Book.rating_count, 0)).desc(),
            func.round(func.avg(Book.rating_avg), 1).desc(),
        )
    else:  # release_date_desc (default)
        groups_q = groups_q.order_by(func.max(Book.release_date).desc())

    # Total count via subquery
    count_sub = groups_q.subquery()
    total_count = session.query(func.count()).select_from(count_sub).scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    page_groups = groups_q.limit(page_size).offset(offset).all()

    return page_groups, total_count


def _load_card_details(session, page_groups):
    """Load detail data for the page's card groups and build Card objects.

    For series cards: load Series row + first book (by position_in_series) with authors.
    For standalone cards: load Book with authors + narrators.
    Returns cards in the same order as page_groups.
    """
    if not page_groups:
        return []

    # Separate series and standalone groups
    series_groups = []  # (group_key=series_id, row)
    standalone_groups = []  # (group_key=book_id, row)
    for row in page_groups:
        if row.card_type == "series":
            series_groups.append(row)
        else:
            standalone_groups.append(row)

    # --- Load standalone books ---
    standalone_map: dict[int, Book] = {}
    if standalone_groups:
        standalone_ids = [r.group_key for r in standalone_groups]
        books = (
            session.query(Book)
            .options(
                selectinload(Book.book_authors).selectinload(BookAuthor.person),
                selectinload(Book.book_narrators).selectinload(BookNarrator.person),
            )
            .filter(Book.id.in_(standalone_ids))
            .all()
        )
        standalone_map = {b.id: b for b in books}

    # --- Load series data ---
    series_map: dict[int, Series] = {}
    series_first_book_map: dict[int, Book] = {}
    series_book_counts: dict[int, int] = {}
    series_rating_data: dict[int, tuple[float | None, int]] = {}

    if series_groups:
        series_ids = [r.group_key for r in series_groups]

        # Load Series objects
        series_rows = session.query(Series).filter(Series.id.in_(series_ids)).all()
        series_map = {s.id: s for s in series_rows}

        # Store aggregated data from the group query
        for row in series_groups:
            series_book_counts[row.group_key] = row.book_count
            series_rating_data[row.group_key] = (row.avg_rating, int(row.total_rating_count))

        # Find first book per series (lowest position_in_series, fallback to min book_id)
        # Get all book_series rows for these series
        bs_rows = (
            session.query(BookSeries)
            .filter(BookSeries.series_id.in_(series_ids))
            # Only consider books that passed filters — join with filtered books
            .join(Book, Book.id == BookSeries.book_id)
            .filter(Book.art_type == 1)
            .all()
        )

        # Group by series_id and pick first book
        series_bs: dict[int, list[BookSeries]] = {}
        for bs in bs_rows:
            series_bs.setdefault(bs.series_id, []).append(bs)

        first_book_ids = []
        for sid in series_ids:
            if sid in series_bs:
                bs_list = series_bs[sid]
                # Sort by position (None → 999999), then by book_id
                bs_list.sort(key=lambda x: (x.position_in_series if x.position_in_series is not None else 999999, x.book_id))
                first_book_ids.append(bs_list[0].book_id)

        if first_book_ids:
            first_books = (
                session.query(Book)
                .options(
                    selectinload(Book.book_authors).selectinload(BookAuthor.person),
                )
                .filter(Book.id.in_(first_book_ids))
                .all()
            )
            first_book_map = {b.id: b for b in first_books}

            for sid in series_ids:
                if sid in series_bs:
                    bs_list = series_bs[sid]
                    bs_list.sort(key=lambda x: (x.position_in_series if x.position_in_series is not None else 999999, x.book_id))
                    fb_id = bs_list[0].book_id
                    if fb_id in first_book_map:
                        series_first_book_map[sid] = first_book_map[fb_id]

    # --- Build cards in order ---
    cards: list[Card] = []
    for row in page_groups:
        if row.card_type == "series":
            sid = row.group_key
            series = series_map.get(sid)
            first_book = series_first_book_map.get(sid)
            if series is None:
                continue

            avg_rating, total_rc = series_rating_data.get(sid, (None, 0))

            cards.append(SeriesCard(
                type="series",
                title=series.name,
                cover_url=first_book.cover_url if first_book else None,
                url=_abs_url(series.url) if series.url else (_abs_url(first_book.url) if first_book else ""),
                rating_avg=avg_rating,
                rating_count=total_rc,
                series_id=sid,
                book_count=row.book_count,
                authors=_sorted_author_names(first_book.book_authors) if first_book else [],
                release_date=row.max_release_date,
            ))
        else:
            book = standalone_map.get(row.group_key)
            if book is None:
                continue
            cards.append(BookCard(
                type="book",
                title=book.title,
                cover_url=book.cover_url,
                url=_abs_url(book.url),
                rating_avg=book.rating_avg,
                rating_count=book.rating_count or 0,
                book_id=book.id,
                authors=_sorted_author_names(book.book_authors),
                narrator=_first_narrator_name(book.book_narrators),
                release_date=book.release_date,
            ))

    return cards


def _handle_f11_incomplete_series(session, base_q, query, user_id):
    """F-11 special path: incomplete series only.

    Uses Python post-filter since the result set is small (only user's listened series).
    Batch-loads all book_series rows to avoid N+1.
    """
    # Get all filtered book IDs as subquery
    filtered_ids = base_q.with_entities(Book.id).subquery().select()

    # Group by series_id only (F-11 pre-filter ensures only series books)
    groups_q = (
        session.query(
            BookSeries.series_id.label("group_key"),
            literal("series").label("card_type"),
            func.max(Book.release_date).label("max_release_date"),
            func.avg(Book.rating_avg).label("avg_rating"),
            func.sum(func.coalesce(Book.rating_count, 0)).label("total_rating_count"),
            func.count().label("book_count"),
        )
        .select_from(Book)
        .join(BookSeries, BookSeries.book_id == Book.id)
        .filter(Book.id.in_(filtered_ids))
        .group_by(BookSeries.series_id)
    )

    all_groups = groups_q.all()
    if not all_groups:
        return CatalogResult(cards=[], total_count=0, page=query.page,
                             page_size=query.page_size, has_next=False)

    # Batch-load all book_series rows for candidate series (fix N+1)
    candidate_series_ids = [g.group_key for g in all_groups]
    all_bs_rows = (
        session.query(BookSeries)
        .filter(BookSeries.series_id.in_(candidate_series_ids))
        .all()
    )
    series_book_ids: dict[int, set[int]] = {}
    for bs in all_bs_rows:
        series_book_ids.setdefault(bs.series_id, set()).add(bs.book_id)

    # Get listened book IDs
    listened_ids = set(
        row[0]
        for row in session.query(UserListenedBook.book_id)
        .filter(UserListenedBook.user_id == user_id)
        .all()
    )

    # Filter: keep only series with both listened and unlistened books
    kept_series_ids = set()
    for sid, book_ids in series_book_ids.items():
        has_listened = bool(book_ids & listened_ids)
        has_unlistened = bool(book_ids - listened_ids)
        if has_listened and has_unlistened:
            kept_series_ids.add(sid)

    filtered_groups = [g for g in all_groups if g.group_key in kept_series_ids]

    # Sort in Python
    _min_date = datetime(1900, 1, 1)
    sort_key = query.sort
    if sort_key == "rating_avg_desc":
        filtered_groups.sort(
            key=lambda g: (round(g.avg_rating, 1) if g.avg_rating else 0, int(g.total_rating_count)),
            reverse=True,
        )
    elif sort_key == "rating_count_desc":
        filtered_groups.sort(
            key=lambda g: (int(g.total_rating_count), round(g.avg_rating, 1) if g.avg_rating else 0),
            reverse=True,
        )
    else:  # release_date_desc
        filtered_groups.sort(
            key=lambda g: g.max_release_date or _min_date,
            reverse=True,
        )

    # Paginate in Python
    total_count = len(filtered_groups)
    start = (query.page - 1) * query.page_size
    end = start + query.page_size
    page_groups = filtered_groups[start:end]
    has_next = end < total_count

    # Load details for the page
    cards = _load_card_details(session, page_groups)

    return CatalogResult(
        cards=cards,
        total_count=total_count,
        page=query.page,
        page_size=query.page_size,
        has_next=has_next,
    )


def get_catalog(
    session: Session,
    query: CatalogQuery,
    user_id: int = 1,
) -> CatalogResult:
    """Build filtered, sorted, paginated catalog.

    Returns CatalogResult with series-grouped cards.
    Uses SQL-level GROUP BY with LIMIT/OFFSET for pagination,
    then loads detail data only for the page's cards.
    """
    base_q = _build_filtered_book_query(session, query, user_id)

    # F-11 special path
    if query.incomplete_series_only:
        return _handle_f11_incomplete_series(session, base_q, query, user_id)

    # Normal path: SQL-level grouping + pagination
    page_groups, total_count = _get_paginated_card_groups(
        session, base_q, query.sort, query.page, query.page_size,
    )

    cards = _load_card_details(session, page_groups)

    has_next = (query.page * query.page_size) < total_count

    return CatalogResult(
        cards=cards,
        total_count=total_count,
        page=query.page,
        page_size=query.page_size,
        has_next=has_next,
    )
