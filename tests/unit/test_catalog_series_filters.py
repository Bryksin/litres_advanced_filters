"""Series-level catalog filters: series size, full subscription, F-11 (Phase 16).

Regression tests for:
- series size counted only on genre-tagged books (a series with books tagged
  in a sibling genre was judged too short under a genre filter);
- full-series-subscription removing a book from every series it belongs to,
  even a fully subscribed one;
- F-11 (incomplete series only) ignoring series_min/series_max/rating_count_min.
"""

from datetime import datetime, timezone

import pytest

from app.db.models import (
    Book, BookGenre, BookNarrator, BookSeries, Genre, Person, Series, User,
    UserListenedBook,
)
from app.models.catalog_query import CatalogQuery
from app.services.catalog_service import get_catalog

AI_NARRATOR = "Литрес Авточтец"


def _book(s, book_id, *, genre, series=None, pos=None, sub=True, rating_count=10,
          narrator=None):
    now = datetime.now(timezone.utc)
    s.add(Book(
        id=book_id, title=f"Book {book_id}", url=f"/audiobook/{book_id}/", art_type=1,
        rating_avg=4.0, rating_count=rating_count,
        release_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_available_with_subscription=sub, cached_at=now,
    ))
    s.add(BookGenre(book_id=book_id, genre_id=genre, cached_at=now))
    if series is not None:
        s.add(BookSeries(book_id=book_id, series_id=series, position_in_series=pos))
    if narrator is not None:
        s.add(BookNarrator(book_id=book_id, person_id=narrator))


@pytest.fixture
def s(db_session):
    s = db_session
    s.add(User(id=1, created_at=datetime.now(timezone.utc)))
    s.add(Genre(id="space", name="Space", slug="space", url="/genre/space/"))
    s.add(Genre(id="combat", name="Combat", slug="combat", url="/genre/combat/"))
    s.add(Person(id=1, full_name=AI_NARRATOR, url="/p/1/"))
    for sid in (1, 2, 3, 4):
        s.add(Series(id=sid, name=f"Series {sid}", url=f"/series/s-{sid}/", book_count=0))

    # Series 1: 4 books, only book 1 tagged "space", books 2-4 tagged "combat".
    _book(s, 101, genre="space", series=1, pos=1)
    for i, bid in enumerate((102, 103, 104), start=2):
        _book(s, bid, genre="combat", series=1, pos=i)

    # Series 2: 4 books all tagged "space", fully subscribed. Book 201 also
    # belongs to series 3, which has a non-subscription book.
    for i, bid in enumerate((201, 202, 203, 204), start=1):
        _book(s, bid, genre="space", series=2, pos=i)
    s.add(BookSeries(book_id=201, series_id=3, position_in_series=1))
    _book(s, 301, genre="space", series=3, pos=2, sub=False)

    # Series 4: 4 "space" books, one narrated by the AI narrator.
    for i, bid in enumerate((401, 402, 403, 404), start=1):
        _book(s, bid, genre="space", series=4, pos=i, narrator=1 if bid == 404 else None)

    s.flush()
    return s


def _series_ids(result):
    return sorted(c.series_id for c in result.cards if c.type == "series")


def test_series_size_ignores_genre_tagging(s):
    """A 4-book series passes min 4 under a genre that tags only one of its books."""
    result = get_catalog(s, CatalogQuery(genre_id="space", series_only=True, series_min=4))
    assert 1 in _series_ids(result)
    card = next(c for c in result.cards if c.series_id == 1)
    assert card.book_count == 4  # filter agrees with what the card shows


def test_series_max_ignores_genre_tagging(s):
    """series_max uses the same size: a 4-book series fails max 3 under the genre."""
    result = get_catalog(s, CatalogQuery(genre_id="space", series_only=True, series_max=3))
    assert 1 not in _series_ids(result)


def test_series_size_still_counts_after_narrator_exclusion(s):
    """Narrator exclusion still shrinks the size (deliberate since v1.0.1)."""
    q = CatalogQuery(genre_id="space", series_only=True, series_min=4,
                     exclude_narrators=True, excluded_narrators=[AI_NARRATOR])
    assert 4 not in _series_ids(get_catalog(s, q))
    q.exclude_narrators = False
    assert 4 in _series_ids(get_catalog(s, q))


def test_series_min_drops_standalones_and_max_keeps_them(s):
    _book(s, 900, genre="space")
    s.flush()
    no_series_only = get_catalog(s, CatalogQuery(genre_id="space", series_min=2))
    assert all(c.type == "series" for c in no_series_only.cards)
    with_max = get_catalog(s, CatalogQuery(genre_id="space", series_max=10))
    assert any(c.type == "book" and c.book_id == 900 for c in with_max.cards)


def test_full_subscription_keeps_shared_book_in_fully_subscribed_series(s):
    """Book 201 is in series 2 (all subscription) and series 3 (has a paid book).

    Series 3 must be dropped; series 2 must keep all 4 books, so it still passes min 4.
    """
    q = CatalogQuery(genre_id="space", series_only=True, full_series_subscription=True,
                     series_min=4)
    ids = _series_ids(get_catalog(s, q))
    assert 2 in ids
    assert 3 not in ids


def test_full_subscription_drops_partial_series(s):
    q = CatalogQuery(series_only=True, full_series_subscription=True)
    assert 3 not in _series_ids(get_catalog(s, q))
    q.full_series_subscription = False
    assert 3 in _series_ids(get_catalog(s, q))


def _listen(s, *book_ids):
    for bid in book_ids:
        s.add(UserListenedBook(user_id=1, book_id=bid,
                               listened_at=datetime.now(timezone.utc)))
    s.flush()


def test_f11_respects_series_min(s):
    """Incomplete-series mode applies the series size filter."""
    _listen(s, 101, 301)  # series 1 (4 books) and series 3 (2 books) partly listened
    base = dict(series_only=True, incomplete_series_only=True)
    assert _series_ids(get_catalog(s, CatalogQuery(**base))) == [1, 3]
    assert _series_ids(get_catalog(s, CatalogQuery(**base, series_min=4))) == [1]
    assert _series_ids(get_catalog(s, CatalogQuery(**base, series_max=3))) == [3]


def test_f11_respects_rating_count_min(s):
    _listen(s, 101, 301)  # series 1: 4 x 10 votes = 40; series 3: 2 x 10 = 20
    q = CatalogQuery(series_only=True, incomplete_series_only=True, rating_count_min=30)
    assert _series_ids(get_catalog(s, q)) == [1]
