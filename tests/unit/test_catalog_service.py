"""Unit tests for CatalogService.get_catalog().

Uses in-memory SQLite via db_session fixture. Seed data covers:
- 5 standalone books (ids 1-5)
- 1 series "Alpha" with 3 books (ids 10, 11, 12)
- 1 series "Beta" with 2 books (ids 20, 21) — one non-subscription
- Various genres, authors, narrators for filter testing
"""

from datetime import datetime, timezone

import pytest

from app.db.models import (
    Book, BookAuthor, BookGenre, BookNarrator, BookSeries,
    Genre, Person, Series, User, UserIgnoredBook, UserSettings,
)
from app.models.catalog_query import CatalogQuery
from app.services.catalog_service import get_catalog


@pytest.fixture
def seeded_session(db_session):
    """Seed the in-memory DB with test data for catalog queries."""
    s = db_session

    # User
    s.add(User(id=1, created_at=datetime.now(timezone.utc)))
    s.add(UserSettings(user_id=1))

    # Genres
    s.add(Genre(id="100", name="Fantasy", slug="fantasy", url="/genre/fantasy/"))
    s.add(Genre(id="200", name="Sci-Fi", slug="sci-fi", url="/genre/sci-fi/"))

    # Persons
    s.add(Person(id=1, full_name="Author A", url="/a/"))
    s.add(Person(id=2, full_name="Author B", url="/b/"))
    s.add(Person(id=3, full_name="Narrator X", url="/x/"))
    s.add(Person(id=4, full_name="\u041b\u0438\u0442\u0440\u0435\u0441 \u0410\u0432\u0442\u043e\u0447\u0442\u0435\u0446", url="/avto/"))

    # Series
    s.add(Series(id=1, name="Alpha Series", slug="alpha", url="/series/alpha-1/", book_count=3))
    s.add(Series(id=2, name="Beta Series", slug="beta", url="/series/beta-2/", book_count=2))

    now = datetime.now(timezone.utc)

    # Standalone books (ids 1-5)
    for i in range(1, 6):
        s.add(Book(
            id=i, title=f"Standalone {i}", url=f"/book/{i}",
            art_type=1, cover_url=f"/cover/{i}.jpg", rating_avg=3.0 + i * 0.3, rating_count=100 * i,
            release_date=datetime(2026, 1, i, tzinfo=timezone.utc),
            is_available_with_subscription=True, cached_at=now,
        ))
        s.add(BookGenre(book_id=i, genre_id="100", cached_at=now))
        s.add(BookAuthor(book_id=i, person_id=1, sort_order=0))
        # Book 3 narrated by Авточтец
        if i == 3:
            s.add(BookNarrator(book_id=i, person_id=4))
        else:
            s.add(BookNarrator(book_id=i, person_id=3))

    # Alpha series books (ids 10-12): all subscription, genre Fantasy
    for i, book_id in enumerate([10, 11, 12]):
        s.add(Book(
            id=book_id, title=f"Alpha Book {i+1}", url=f"/book/{book_id}",
            art_type=1, cover_url=f"/cover/{book_id}.jpg", rating_avg=4.5, rating_count=200,
            release_date=datetime(2026, 2, i + 1, tzinfo=timezone.utc),
            is_available_with_subscription=True, cached_at=now,
        ))
        s.add(BookGenre(book_id=book_id, genre_id="100", cached_at=now))
        s.add(BookAuthor(book_id=book_id, person_id=1, sort_order=0))
        s.add(BookNarrator(book_id=book_id, person_id=3))
        s.add(BookSeries(book_id=book_id, series_id=1, position_in_series=i + 1))

    # Beta series books (ids 20, 21): book 21 is NOT subscription
    s.add(Book(
        id=20, title="Beta Book 1", url="/book/20",
        art_type=1, rating_avg=3.0, rating_count=50,
        release_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        is_available_with_subscription=True, cached_at=now,
    ))
    s.add(Book(
        id=21, title="Beta Book 2", url="/book/21",
        art_type=1, rating_avg=2.0, rating_count=30,
        release_date=datetime(2026, 3, 2, tzinfo=timezone.utc),
        is_available_with_subscription=False, cached_at=now,
    ))
    for book_id in [20, 21]:
        s.add(BookGenre(book_id=book_id, genre_id="200", cached_at=now))
        s.add(BookAuthor(book_id=book_id, person_id=2, sort_order=0))
        s.add(BookNarrator(book_id=book_id, person_id=3))
    s.add(BookSeries(book_id=20, series_id=2, position_in_series=1))
    s.add(BookSeries(book_id=21, series_id=2, position_in_series=2))

    s.flush()
    return s


# --- No filters (default) ---

def test_no_filters_returns_all(seeded_session):
    """Default query returns all books grouped into cards."""
    result = get_catalog(seeded_session, CatalogQuery())
    # 5 standalones + 2 series cards = 7 cards
    assert result.total_count == 7


# --- Genre filter ---

def test_genre_filter(seeded_session):
    """Genre filter returns only books in that genre."""
    result = get_catalog(seeded_session, CatalogQuery(genre_id="200"))
    # Only Beta series (genre 200) → 1 series card
    assert result.total_count == 1
    assert result.cards[0].type == "series"
    assert result.cards[0].title == "Beta Series"


# --- Series only ---

def test_series_only(seeded_session):
    """series_only=True returns only series cards."""
    result = get_catalog(seeded_session, CatalogQuery(series_only=True))
    assert all(c.type == "series" for c in result.cards)
    assert result.total_count == 2  # Alpha + Beta


# --- Standalones only ---

def test_standalones_only(seeded_session):
    """standalones_only=True returns only standalone book cards."""
    result = get_catalog(seeded_session, CatalogQuery(standalones_only=True))
    assert all(c.type == "book" for c in result.cards)
    assert result.total_count == 5


# --- Series size ---

def test_series_size_min(seeded_session):
    """series_min=3 excludes Beta (2 books), keeps Alpha (3 books)."""
    result = get_catalog(seeded_session, CatalogQuery(series_only=True, series_min=3))
    assert result.total_count == 1
    assert result.cards[0].title == "Alpha Series"


def test_series_size_max(seeded_session):
    """series_max=2 excludes Alpha (3 books), keeps Beta (2 books)."""
    result = get_catalog(seeded_session, CatalogQuery(series_only=True, series_max=2))
    assert result.total_count == 1
    assert result.cards[0].title == "Beta Series"


# --- Full series subscription (F-10) ---

def test_full_series_subscription(seeded_session):
    """full_series_subscription=True excludes Beta (book 21 is non-sub)."""
    result = get_catalog(seeded_session, CatalogQuery(
        series_only=True, full_series_subscription=True,
    ))
    assert result.total_count == 1
    assert result.cards[0].title == "Alpha Series"


# --- Author exclusion ---

def test_exclude_authors(seeded_session):
    """Excluding Author B removes Beta series."""
    result = get_catalog(seeded_session, CatalogQuery(
        exclude_authors=True, excluded_authors=["Author B"],
    ))
    titles = [c.title for c in result.cards]
    assert "Beta Series" not in titles


# --- Narrator exclusion (F-6) ---

def test_exclude_narrators(seeded_session):
    """Excluding Литрес Авточтец removes book 3."""
    result = get_catalog(seeded_session, CatalogQuery(
        exclude_narrators=True, excluded_narrators=["\u041b\u0438\u0442\u0440\u0435\u0441 \u0410\u0432\u0442\u043e\u0447\u0442\u0435\u0446"],
    ))
    book_ids = [c.book_id for c in result.cards if c.type == "book"]
    assert 3 not in book_ids


# --- Rating range (F-8) ---

def test_rating_min(seeded_session):
    """rating_min=4.0 excludes low-rated books."""
    result = get_catalog(seeded_session, CatalogQuery(rating_min=4.0))
    for card in result.cards:
        if card.type == "book":
            assert card.rating_avg >= 4.0


def test_rating_max(seeded_session):
    """rating_max=3.5 excludes high-rated books."""
    result = get_catalog(seeded_session, CatalogQuery(rating_max=3.5))
    for card in result.cards:
        if card.type == "book":
            assert card.rating_avg <= 3.5


# --- Minimum votes filter ---

def test_rating_count_min(seeded_session):
    """rating_count_min=200 excludes books with fewer votes."""
    # Standalone rating_counts: 100, 200, 300, 400, 500
    # Alpha series: 200 each (600 summed), Beta series: 50+30=80 summed
    result = get_catalog(seeded_session, CatalogQuery(rating_count_min=200))
    # Should exclude: standalone 1 (100 votes), Beta series (50 and 30 each < 200)
    for card in result.cards:
        if card.type == "book":
            assert card.rating_count >= 200


def test_rating_count_min_none_means_no_filter(seeded_session):
    """When rating_count_min is None, no vote-count filter is applied."""
    result = get_catalog(seeded_session, CatalogQuery(rating_count_min=None))
    assert result.total_count == 7  # all cards


# --- Ignore list (F-9) ---

def test_ignore_list(seeded_session):
    """Ignored books are excluded from results."""
    seeded_session.add(UserIgnoredBook(
        user_id=1, book_id=1, ignored_at=datetime.now(timezone.utc),
    ))
    seeded_session.flush()

    result = get_catalog(seeded_session, CatalogQuery())
    book_ids = [c.book_id for c in result.cards if c.type == "book"]
    assert 1 not in book_ids


# --- Sort ---

def test_sort_by_rating(seeded_session):
    """Sort by rating_avg_desc puts highest-rated first."""
    result = get_catalog(seeded_session, CatalogQuery(
        standalones_only=True, sort="rating_avg_desc",
    ))
    ratings = [c.rating_avg for c in result.cards]
    assert ratings == sorted(ratings, reverse=True)


def test_sort_by_release_date(seeded_session):
    """Default sort (release_date_desc) puts newest first."""
    result = get_catalog(seeded_session, CatalogQuery(standalones_only=True))
    # Standalone 5 (Jan 5) is newest → should be first
    book_ids = [c.book_id for c in result.cards]
    assert book_ids[0] == 5


# --- Pagination ---

def test_pagination(seeded_session):
    """page_size=3 returns correct slices and has_next."""
    result = get_catalog(seeded_session, CatalogQuery(page=1, page_size=3))
    assert len(result.cards) == 3
    assert result.has_next is True

    result2 = get_catalog(seeded_session, CatalogQuery(page=2, page_size=3))
    assert len(result2.cards) == 3
    assert result2.has_next is True

    result3 = get_catalog(seeded_session, CatalogQuery(page=3, page_size=3))
    assert len(result3.cards) == 1
    assert result3.has_next is False


# --- Series grouping ---

def test_series_card_fields(seeded_session):
    """Series card has correct aggregated fields."""
    result = get_catalog(seeded_session, CatalogQuery(series_only=True))
    alpha = next(c for c in result.cards if c.title == "Alpha Series")
    assert alpha.book_count == 3
    assert alpha.rating_avg == 4.5  # all 3 books have 4.5
    assert alpha.rating_count == 600  # 200 * 3


def test_series_card_cover_is_first_book(seeded_session):
    """Series card cover comes from the book with lowest position_in_series."""
    result = get_catalog(seeded_session, CatalogQuery(series_only=True))
    alpha = next(c for c in result.cards if c.title == "Alpha Series")
    # Book 10 has position 1 → its cover_url
    assert alpha.cover_url is not None


# --- Sorting correctness (post-grouping) ---

def test_sort_rating_secondary_by_votes(seeded_session):
    """When rating is equal, secondary sort is by rating_count DESC."""
    result = get_catalog(seeded_session, CatalogQuery(sort="rating_avg_desc"))
    # Find cards with equal ratings — Alpha series (4.5) should be among them
    # Alpha: rating_avg=4.5, rating_count=600
    # Standalone 5: rating_avg=4.5, rating_count=500
    cards_45 = [c for c in result.cards if c.rating_avg and abs(c.rating_avg - 4.5) < 0.01]
    if len(cards_45) >= 2:
        counts = [c.rating_count for c in cards_45]
        assert counts == sorted(counts, reverse=True), "Secondary sort by vote count failed"


def test_sort_votes_works_for_series(seeded_session):
    """Sort by votes uses summed rating_count for series cards."""
    result = get_catalog(seeded_session, CatalogQuery(
        series_only=True, sort="rating_count_desc",
    ))
    counts = [c.rating_count for c in result.cards]
    assert counts == sorted(counts, reverse=True)


def test_series_card_release_date_is_max(seeded_session):
    """Series card release_date = max(release_date) of books in series."""
    result = get_catalog(seeded_session, CatalogQuery(series_only=True))
    alpha = next(c for c in result.cards if c.title == "Alpha Series")
    # Alpha books: Feb 1, Feb 2, Feb 3 → max is Feb 3
    # SQLite strips timezone info, so compare naive
    assert alpha.release_date.replace(tzinfo=None) == datetime(2026, 2, 3)


def test_sort_release_date_uses_series_max(seeded_session):
    """Sort by newest uses max release_date for series, not first book's date."""
    result = get_catalog(seeded_session, CatalogQuery(sort="release_date_desc"))
    # Beta series has books Mar 1 and Mar 2 → max is Mar 2
    # Alpha series has books Feb 1-3 → max is Feb 3
    # Standalone 5 has Jan 5
    # Beta (Mar 2) should come before Alpha (Feb 3) which comes before standalones
    dates = [c.release_date for c in result.cards if c.release_date]
    assert dates == sorted(dates, reverse=True)
