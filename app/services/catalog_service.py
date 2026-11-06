"""CatalogService — filtered, sorted, paginated catalog queries.

All data comes from the sync-populated DB. Zero HTTP calls to LitRes.

Two-query approach:
1. One SQL query returns the page's card groups (LIMIT/OFFSET) plus the total
   count (window function), see _get_paginated_card_groups()
2. Load detail data only for the page's cards

Card semantics (Phase 16 rulings):
- Filters decide only WHICH cards appear. A series card's rating, votes, date
  and book count describe the whole series (all its audiobooks), and the sort
  and the "min votes" filter use those whole-series numbers.
- A book with rating_count = 0 is unrated: it is left out of rating averages
  and fails a set rating_min/rating_max bound. A series with no rated book has
  no rating (shown as "—") and sorts last under the rating sort.
"""

import dataclasses
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import and_, case, false, func, literal, or_, true
from sqlalchemy.orm import Session, aliased, selectinload

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
from app.services.settings_service import normalize_name

log = logging.getLogger(__name__)

_LITRES_BASE = "https://www.litres.ru"


def _abs_url(path: str) -> str:
    if path.startswith("http"):
        return path
    return _LITRES_BASE + path


def _series_url(raw_url: str) -> str:
    """Build a series URL that filters to audiobooks available by subscription."""
    url = _abs_url(raw_url)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params["art_types"] = ["audiobook"]
    params["only_litres_subscription_arts"] = ["true"]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _sorted_author_names(book_authors: list[BookAuthor]) -> list[str]:
    return [ba.person.full_name for ba in sorted(book_authors, key=lambda x: x.sort_order)]


def _first_narrator_name(book_narrators: list[BookNarrator]) -> str | None:
    """Deterministic narrator for a standalone card (lowest person id).

    book_narrator has no order column, so row order is whatever SQLite returns
    and can change after a re-sync rewrites the junction rows.
    """
    if not book_narrators:
        return None
    return min(book_narrators, key=lambda bn: bn.person_id).person.full_name


def _person_ids_matching(session: Session, names: list[str]) -> list[int]:
    """IDs of persons whose name matches one of ``names`` case-insensitively.

    Matching uses normalize_name() (whitespace-collapsed, casefold, ё→е) in
    Python, because SQLite's lower() does not fold Cyrillic. The result is
    memoised on the session, which lives for one request.
    """
    wanted = frozenset(normalize_name(n) for n in names if isinstance(n, str) and n.strip())
    if not wanted:
        return []
    cache = session.info.setdefault("_person_ids_by_names", {})
    if wanted not in cache:
        cache[wanted] = [
            pid for pid, full_name in session.query(Person.id, Person.full_name)
            if full_name and normalize_name(full_name) in wanted
        ]
    return cache[wanted]


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

    # Series size (series_min/series_max) and full series under subscription (F-10)
    # are series-level properties; they are applied on BookSeries rows during
    # grouping (see _series_level_filter), not here on books.

    # Author exclusion (case-insensitive, see _person_ids_matching)
    if query.exclude_authors and query.excluded_authors:
        person_ids = _person_ids_matching(session, query.excluded_authors)
        if person_ids:
            q = q.filter(~Book.id.in_(
                session.query(BookAuthor.book_id).filter(BookAuthor.person_id.in_(person_ids))
            ))

    # Narrator exclusion (F-6)
    if query.exclude_narrators and query.excluded_narrators:
        person_ids = _person_ids_matching(session, query.excluded_narrators)
        if person_ids:
            q = q.filter(~Book.id.in_(
                session.query(BookNarrator.book_id).filter(BookNarrator.person_id.in_(person_ids))
            ))

    # Rating range (F-8). Unrated books (no votes) fail any set bound.
    if query.rating_min is not None or query.rating_max is not None:
        q = q.filter(Book.rating_count > 0)
    if query.rating_min is not None:
        q = q.filter(Book.rating_avg >= query.rating_min)
    if query.rating_max is not None:
        q = q.filter(Book.rating_avg <= query.rating_max)
    # rating_count_min is applied on the card's (whole-series) vote total in
    # _get_paginated_card_groups(), matching the displayed number.

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


def _listened_book_ids(session: Session, user_id: int):
    return session.query(UserListenedBook.book_id).filter(UserListenedBook.user_id == user_id)


def _series_level_filter(session: Session, query: CatalogQuery, user_id: int):
    """WHERE clause on the (Book LEFT JOIN BookSeries) membership rows, or None.

    Applies the filters that describe a whole series rather than a single book:

    - Series size (series_min / series_max) is the number of the series'
      audiobooks that pass all book filters EXCEPT the genre and the listened
      filters. The genre selects which series are shown; it must not shorten a
      series whose other books carry a different genre tag. Books the user has
      listened to still count (hide-listened only hides their cards); excluded
      narrators/authors, the rating range and the ignore list do shrink it.
      Standalones have size 1.
    - Full series under subscription (F-10) drops a series when any member is
      not available by subscription, except books the user has already
      listened to. It works per series, so a book that also belongs to another,
      fully subscribed series stays in that other series' card.
    - Incomplete series only (F-11) keeps a series only when at least one book
      the user has NOT listened to passes the user's filters (genre aside), and
      at least one book of that series has been listened to.
    """
    clauses = []
    non_genre_query = dataclasses.replace(
        query, genre_id=None, hide_listened=False, incomplete_series_only=False,
    )

    if query.series_min is not None or query.series_max is not None:
        size_ids = _build_filtered_book_query(session, non_genre_query, user_id).with_entities(Book.id)
        sized_series = (
            session.query(BookSeries.series_id)
            .filter(BookSeries.book_id.in_(size_ids))
            .group_by(BookSeries.series_id)
        )
        if query.series_min is not None:
            sized_series = sized_series.having(func.count() >= query.series_min)
        if query.series_max is not None:
            sized_series = sized_series.having(func.count() <= query.series_max)

        standalone_ok = (
            (query.series_min is None or query.series_min <= 1)
            and (query.series_max is None or query.series_max >= 1)
        )
        clauses.append(or_(
            and_(BookSeries.series_id.is_(None), true() if standalone_ok else false()),
            BookSeries.series_id.in_(sized_series),
        ))

    if query.series_only and query.full_series_subscription:
        series_with_non_sub = (
            session.query(BookSeries.series_id)
            .join(Book, Book.id == BookSeries.book_id)
            .filter(Book.is_available_with_subscription == False)  # noqa: E712
            .filter(~Book.id.in_(_listened_book_ids(session, user_id)))
        )
        clauses.append(or_(
            BookSeries.series_id.is_(None),
            BookSeries.series_id.not_in(series_with_non_sub),
        ))

    if query.incomplete_series_only:
        passing_ids = _build_filtered_book_query(session, non_genre_query, user_id).with_entities(Book.id)
        series_with_unlistened = (
            session.query(BookSeries.series_id)
            .filter(BookSeries.book_id.in_(passing_ids))
            .filter(~BookSeries.book_id.in_(_listened_book_ids(session, user_id)))
        )
        # The book pre-filter admits a book of a listened series; that book may
        # also belong to another series, which must itself have a listened book.
        series_with_listened = (
            session.query(BookSeries.series_id)
            .filter(BookSeries.book_id.in_(_listened_book_ids(session, user_id)))
        )
        clauses.append(and_(
            BookSeries.series_id.in_(series_with_listened),
            BookSeries.series_id.in_(series_with_unlistened),
        ))

    return and_(*clauses) if clauses else None


def _get_paginated_card_groups(session, base_q, sort_key, page, page_size,
                               series_filter=None, rating_count_min=None):
    """SQL-level grouping and pagination in one query.

    1. ``members``: distinct card keys of the filtered books — series_id for a
       series book (one row per series it belongs to), book.id for a standalone
       — restricted by the series-level filter.
    2. Whole-series numbers for the member series (all audiobooks of the
       series, regardless of filters); a standalone uses its own book.
    3. "Min votes" on the card's total, sort, LIMIT/OFFSET, and the total card
       count as a window function, so the grouping runs once per page load.

    Returns (page_groups, total_count). Rows carry group_key, card_type,
    max_release_date, avg_rating (NULL when no book is rated),
    total_rating_count, book_count.
    """
    filtered_ids = base_q.with_entities(Book.id).subquery().select()

    group_key = func.coalesce(BookSeries.series_id, Book.id)
    card_type = case(
        (BookSeries.series_id.isnot(None), literal("series")),
        else_=literal("book"),
    )
    members_q = (
        session.query(group_key.label("group_key"), card_type.label("card_type"))
        .select_from(Book)
        .outerjoin(BookSeries, BookSeries.book_id == Book.id)
        .filter(Book.id.in_(filtered_ids))
    )
    if series_filter is not None:
        members_q = members_q.filter(series_filter)
    members = members_q.distinct().cte("members")

    member_series_ids = (
        session.query(members.c.group_key).filter(members.c.card_type == "series")
    )
    series_book = aliased(Book)
    series_stats = (
        session.query(
            BookSeries.series_id.label("series_id"),
            func.max(series_book.release_date).label("max_release_date"),
            func.avg(case((series_book.rating_count > 0, series_book.rating_avg), else_=None))
            .label("avg_rating"),
            func.coalesce(func.sum(series_book.rating_count), 0).label("total_rating_count"),
            func.count().label("book_count"),
        )
        .join(series_book, series_book.id == BookSeries.book_id)
        # "+ 0" keeps SQLite off ix_book_art_type (every row has art_type 1, so
        # that index turns this into a full scan: 3.6 s instead of 3 ms on 5077).
        .filter((series_book.art_type + 0) == 1)
        .filter(BookSeries.series_id.in_(member_series_ids))
        .group_by(BookSeries.series_id)
        .subquery("series_stats")
    )
    standalone = aliased(Book)
    is_series = members.c.card_type == "series"

    cards = (
        session.query(
            members.c.group_key.label("group_key"),
            members.c.card_type.label("card_type"),
            case((is_series, series_stats.c.max_release_date),
                 else_=standalone.release_date).label("max_release_date"),
            case((is_series, series_stats.c.avg_rating),
                 else_=case((standalone.rating_count > 0, standalone.rating_avg), else_=None))
            .label("avg_rating"),
            case((is_series, series_stats.c.total_rating_count),
                 else_=func.coalesce(standalone.rating_count, 0)).label("total_rating_count"),
            case((is_series, series_stats.c.book_count), else_=literal(1)).label("book_count"),
        )
        .select_from(members)
        .outerjoin(series_stats, and_(is_series, series_stats.c.series_id == members.c.group_key))
        .outerjoin(standalone, and_(~is_series, standalone.id == members.c.group_key))
        .subquery("cards")
    )

    groups_q = session.query(cards, func.count().over().label("total_count"))

    # Min votes filter — on the card's total (whole series), matching the display
    if rating_count_min is not None:
        groups_q = groups_q.filter(cards.c.total_rating_count >= rating_count_min)

    # Sort. Ratings sort in display buckets (1 decimal; SQL round() matches the
    # Python round() used for display). Unrated cards and undated cards go last.
    # group_key/card_type make the order total, so LIMIT/OFFSET pages never
    # overlap or skip cards that tie on every visible key.
    display_rating = func.round(cards.c.avg_rating, 1)
    unrated_last = cards.c.avg_rating.is_(None)
    tie_break = (cards.c.card_type.asc(), cards.c.group_key.desc())
    if sort_key == "rating_avg_desc":
        order = (unrated_last, display_rating.desc(), cards.c.total_rating_count.desc())
    elif sort_key == "rating_count_desc":
        order = (cards.c.total_rating_count.desc(), unrated_last, display_rating.desc())
    else:  # release_date_desc (default)
        order = (cards.c.max_release_date.is_(None), cards.c.max_release_date.desc())
    groups_q = groups_q.order_by(*order, *tie_break)

    offset = (page - 1) * page_size
    page_groups = groups_q.limit(page_size).offset(offset).all()

    if page_groups:
        total_count = page_groups[0].total_count
    elif page > 1:
        # Past the last page: the window count has no row to ride on.
        total_count = session.query(func.count()).select_from(groups_q.order_by(None).subquery()).scalar() or 0
    else:
        total_count = 0

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

    if series_groups:
        series_ids = [r.group_key for r in series_groups]

        # Load Series objects (the numbers come from the group row: whole series)
        series_rows = session.query(Series).filter(Series.id.in_(series_ids)).all()
        series_map = {s.id: s for s in series_rows}

        # Find first book per series (lowest position_in_series, fallback to min book_id)
        # Get all book_series rows for these series
        bs_rows = (
            session.query(BookSeries)
            .filter(BookSeries.series_id.in_(series_ids))
            # First book of the whole series (spec: cover = first book), not of
            # the filtered subset
            .join(Book, Book.id == BookSeries.book_id)
            .filter((Book.art_type + 0) == 1)  # not via ix_book_art_type, see series_stats
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

            cards.append(SeriesCard(
                type="series",
                title=series.name,
                cover_url=first_book.cover_url if first_book else None,
                url=_series_url(series.url) if series.url else (_series_url(first_book.url) if first_book else ""),
                rating_avg=round(row.avg_rating, 1) if row.avg_rating is not None else None,
                rating_count=int(row.total_rating_count or 0),
                series_id=sid,
                book_count=int(row.book_count),
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
                rating_avg=round(book.rating_avg, 1) if book.rating_count and book.rating_avg is not None else None,
                rating_count=book.rating_count or 0,
                book_id=book.id,
                authors=_sorted_author_names(book.book_authors),
                narrator=_first_narrator_name(book.book_narrators),
                release_date=book.release_date,
            ))

    return cards


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
    series_filter = _series_level_filter(session, query, user_id)

    # F-11 (incomplete series only) uses the same path: its pre-filter keeps
    # only books of series with a listened book (so no standalones), and the
    # "has an unlistened book left" test is part of series_filter.
    page_groups, total_count = _get_paginated_card_groups(
        session, base_q, query.sort, query.page, query.page_size,
        series_filter=series_filter,
        rating_count_min=query.rating_count_min,
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
