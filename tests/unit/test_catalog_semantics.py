"""Catalog card semantics (Phase 16 audit Q1, Q6-Q8, Q10-Q12 and rulings Q-S1..Q-S3).

- Unrated books (rating_count = 0) are left out of rating averages and fail a
  set rating bound.
- Series card numbers describe the whole series; filters decide only which
  series appear; "min votes" uses the whole-series total.
- Listened books are exempt from "whole series under subscription" and still
  count toward series size.
"""

from datetime import datetime, timezone

import pytest

from app.db.models import (
    Book, BookAuthor, BookGenre, BookNarrator, BookSeries, Genre, Person, Series, User,
    UserIgnoredBook, UserListenedBook,
)
from app.models.catalog_query import CatalogQuery
from app.services.catalog_service import get_catalog

AI = "Литрес Авточтец"


def _book(s, book_id, *, genre="space", series=None, pos=None, rating=4.0, votes=10,
          sub=True, day=1, narrators=(), authors=()):
    now = datetime.now(timezone.utc)
    s.add(Book(
        id=book_id, title=f"Book {book_id}", url=f"/audiobook/{book_id}/", art_type=1,
        rating_avg=rating, rating_count=votes,
        release_date=datetime(2026, 1, day, tzinfo=timezone.utc),
        is_available_with_subscription=sub, cached_at=now,
    ))
    if genre:
        s.add(BookGenre(book_id=book_id, genre_id=genre, cached_at=now))
    if series is not None:
        s.add(BookSeries(book_id=book_id, series_id=series, position_in_series=pos))
    for pid in narrators:
        s.add(BookNarrator(book_id=book_id, person_id=pid))
    for i, pid in enumerate(authors):
        s.add(BookAuthor(book_id=book_id, person_id=pid, sort_order=i))


@pytest.fixture
def s(db_session):
    s = db_session
    s.add(User(id=1, created_at=datetime.now(timezone.utc)))
    s.add(Genre(id="space", name="Space", slug="space", url="/genre/space/"))
    s.add(Genre(id="other", name="Other", slug="other", url="/genre/other/"))
    s.add(Person(id=1, full_name=AI, url="/p/1/"))
    s.add(Person(id=2, full_name="Пётр Ёлкин", url="/p/2/"))
    s.add(Person(id=3, full_name="Anna Reader", url="/p/3/"))
    s.add(Person(id=4, full_name="Zed Reader", url="/p/4/"))
    for sid in range(1, 10):
        s.add(Series(id=sid, name=f"Series {sid}", url=f"/series/s-{sid}/", book_count=0))
    s.flush()
    return s


def _cards(result):
    return {(c.series_id if c.type == "series" else -c.book_id): c for c in result.cards}


def _sids(result):
    return sorted(c.series_id for c in result.cards if c.type == "series")


# --- Q1 / Q-S2: unrated books ---------------------------------------------


def test_unrated_books_do_not_drag_series_average_down(s):
    _book(s, 11, series=1, rating=5.0, votes=3)
    for bid in (12, 13, 14):
        _book(s, bid, series=1, rating=0.0, votes=0)
    s.flush()
    card = _cards(get_catalog(s, CatalogQuery(genre_id="space")))[1]
    assert card.rating_avg == 5.0
    assert card.rating_count == 3


def test_series_of_only_unrated_books_has_no_rating_and_sorts_last(s):
    _book(s, 11, series=1, rating=0.0, votes=0)
    _book(s, 21, series=2, rating=2.0, votes=1)
    _book(s, 31, rating=0.0, votes=0)  # unrated standalone
    s.flush()
    r = get_catalog(s, CatalogQuery(genre_id="space", sort="rating_avg_desc"))
    assert [c.rating_avg for c in r.cards] == [2.0, None, None]
    assert _cards(r)[1].rating_avg is None
    assert _cards(r)[-31].rating_avg is None


@pytest.mark.parametrize("bounds", [dict(rating_min=0.0), dict(rating_max=3.0),
                                    dict(rating_min=0.0, rating_max=5.0)])
def test_unrated_book_fails_any_set_rating_bound(s, bounds):
    _book(s, 31, rating=0.0, votes=0)
    _book(s, 32, rating=2.5, votes=4)
    s.flush()
    r = get_catalog(s, CatalogQuery(genre_id="space", **bounds))
    assert [c.book_id for c in r.cards] == [32]


def test_unrated_book_passes_without_rating_bounds(s):
    _book(s, 31, rating=0.0, votes=0)
    s.flush()
    assert get_catalog(s, CatalogQuery(genre_id="space")).total_count == 1


# --- Q-S1 / Q7: whole-series card numbers ---------------------------------


def test_series_card_numbers_cover_whole_series_not_genre_subset(s):
    _book(s, 11, series=1, rating=3.0, votes=1, day=1)                 # tagged space
    _book(s, 12, series=1, genre="other", rating=5.0, votes=99, day=20)  # newest, other genre
    s.flush()
    card = _cards(get_catalog(s, CatalogQuery(genre_id="space")))[1]
    assert card.rating_count == 100
    assert card.rating_avg == 4.0
    assert card.release_date == datetime(2026, 1, 20, tzinfo=timezone.utc)
    assert card.book_count == 2


def test_series_card_numbers_ignore_book_filters(s):
    _book(s, 11, series=1, rating=4.0, votes=10, narrators=(1,))
    _book(s, 12, series=1, rating=2.0, votes=10)
    s.flush()
    q = CatalogQuery(genre_id="space", exclude_narrators=True, excluded_narrators=[AI])
    card = _cards(get_catalog(s, q))[1]
    assert (card.rating_avg, card.rating_count, card.book_count) == (3.0, 20, 2)


def test_newest_sort_uses_whole_series_date(s):
    _book(s, 11, series=1, day=1)
    _book(s, 12, series=1, genre="other", day=25)
    _book(s, 21, series=2, day=10)
    s.flush()
    r = get_catalog(s, CatalogQuery(genre_id="space"))
    assert [c.series_id for c in r.cards] == [1, 2]


def test_min_votes_uses_whole_series_total(s):
    _book(s, 11, series=1, votes=5)
    _book(s, 12, series=1, genre="other", votes=50)
    s.flush()
    assert _sids(get_catalog(s, CatalogQuery(genre_id="space", rating_count_min=40))) == [1]
    assert _sids(get_catalog(s, CatalogQuery(genre_id="space", rating_count_min=60))) == []


# --- Q-S3: listened books exempt from full-subscription -------------------


def _listen(s, *ids):
    for bid in ids:
        s.add(UserListenedBook(user_id=1, book_id=bid, listened_at=datetime.now(timezone.utc)))
    s.flush()


def test_listened_paid_book_does_not_drop_series_under_full_subscription(s):
    _book(s, 11, series=1, sub=False)
    _book(s, 12, series=1)
    _book(s, 21, series=2, sub=False)
    _book(s, 22, series=2)
    _listen(s, 11)
    q = CatalogQuery(genre_id="space", series_only=True, full_series_subscription=True)
    assert _sids(get_catalog(s, q)) == [1]


# --- ESCALATION 1: listened books count toward series size -----------------


def test_hide_listened_does_not_shrink_series_size(s):
    for i, bid in enumerate((11, 12, 13, 14, 15), start=1):
        _book(s, bid, series=1, pos=i)
    _listen(s, 11, 12)
    q = CatalogQuery(genre_id="space", series_only=True, series_min=4, hide_listened=True)
    r = get_catalog(s, q)
    assert _sids(r) == [1]
    assert _cards(r)[1].book_count == 5


def test_hide_listened_still_hides_listened_standalones(s):
    _book(s, 31)
    _book(s, 32)
    _listen(s, 31)
    r = get_catalog(s, CatalogQuery(genre_id="space", hide_listened=True))
    assert [c.book_id for c in r.cards] == [32]


# --- Q6: F-11 considers only unlistened books that pass the filters --------


def test_incomplete_series_ignores_unlistened_books_removed_by_filters(s):
    _book(s, 11, series=1)
    _book(s, 12, series=1)
    _book(s, 21, series=2)
    _book(s, 22, series=2, narrators=(1,))
    _listen(s, 11, 21)
    s.add(UserIgnoredBook(user_id=1, book_id=12, ignored_at=datetime.now(timezone.utc)))
    s.flush()
    q = CatalogQuery(genre_id="space", incomplete_series_only=True)
    assert _sids(get_catalog(s, q)) == [2]  # series 1: the only unlistened book is ignored
    q.exclude_narrators, q.excluded_narrators = True, [AI]
    assert _sids(get_catalog(s, q)) == []   # series 2: the only unlistened book is excluded


def test_incomplete_series_counts_unlistened_book_in_another_genre(s):
    _book(s, 11, series=1)
    _book(s, 12, series=1, genre="other")
    _listen(s, 11)
    assert _sids(get_catalog(s, CatalogQuery(genre_id="space", incomplete_series_only=True))) == [1]


# --- Q8: case-insensitive exclusion --------------------------------------


@pytest.mark.parametrize("typed", ["литрес авточтец", "ЛИТРЕС  АВТОЧТЕЦ", " Литрес Авточтец "])
def test_narrator_exclusion_is_case_and_space_insensitive(s, typed):
    _book(s, 31, narrators=(1,))
    _book(s, 32)
    s.flush()
    q = CatalogQuery(genre_id="space", exclude_narrators=True, excluded_narrators=[typed])
    assert [c.book_id for c in get_catalog(s, q).cards] == [32]


def test_author_exclusion_folds_yo(s):
    _book(s, 31, authors=(2,))
    _book(s, 32)
    s.flush()
    q = CatalogQuery(genre_id="space", exclude_authors=True, excluded_authors=["петр елкин"])
    assert [c.book_id for c in get_catalog(s, q).cards] == [32]


# --- Q10 / Q11: total order and the window count ---------------------------


@pytest.mark.parametrize("sort", ["release_date_desc", "rating_avg_desc", "rating_count_desc"])
def test_full_ties_page_without_duplicates(s, sort):
    for bid in range(100, 130):
        _book(s, bid, rating=0.0, votes=0)  # identical on every sort key
    for sid in (1, 2, 3):
        _book(s, 200 + sid, series=sid, rating=0.0, votes=0)
    s.flush()
    seen = []
    for page in range(1, 40):
        r = get_catalog(s, CatalogQuery(genre_id="space", sort=sort, page=page, page_size=4))
        seen += [(c.type, getattr(c, "series_id", 0) or c.book_id) for c in r.cards]
        assert r.total_count == 33
        if not r.has_next:
            break
    assert len(seen) == 33 == len(set(seen))
    # Deterministic on a full tie: card type, then higher id first
    assert seen[0] == ("book", 129)
    assert seen[-1] == ("series", 1)


def test_total_count_past_last_page(s):
    _book(s, 31)
    s.flush()
    r = get_catalog(s, CatalogQuery(genre_id="space", page=5))
    assert r.cards == [] and r.total_count == 1 and r.has_next is False


# --- Q12: deterministic standalone narrator --------------------------------


def test_standalone_narrator_is_lowest_person_id(s):
    _book(s, 31, narrators=(4, 3))
    s.flush()
    assert get_catalog(s, CatalogQuery(genre_id="space")).cards[0].narrator == "Anna Reader"
