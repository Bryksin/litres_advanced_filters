"""Unit tests for ignore list routes and catalog filtering (F-9).

Tests cover:
- ignore_book creates a UserIgnoredBook row
- unignore_book deletes it
- ignored book is excluded from get_catalog() results
- ignore_series ignores all books in that series
- unignore_series removes ignore for all books in that series
- clear_ignored removes all ignore entries
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.db.models import (
    Book,
    BookAuthor,
    BookGenre,
    BookNarrator,
    BookSeries,
    Genre,
    Person,
    Series,
    User,
    UserIgnoredBook,
    UserSettings,
)
from app.models.catalog_query import CatalogQuery
from app.services.catalog_service import get_catalog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_base(s):
    """Seed user, genre, person — shared across tests."""
    s.add(User(id=1, created_at=datetime.now(timezone.utc)))
    s.add(UserSettings(user_id=1))
    s.add(Genre(id="100", name="Fantasy", slug="fantasy", url="/genre/fantasy/"))
    s.add(Person(id=1, full_name="Author A", url="/a/"))
    s.add(Person(id=2, full_name="Narrator N", url="/n/"))
    s.flush()


def _add_book(s, book_id, title="Book"):
    """Add a standalone audiobook with genre, author, narrator."""
    now = datetime.now(timezone.utc)
    s.add(Book(
        id=book_id, title=f"{title} {book_id}", url=f"/book/{book_id}",
        art_type=1, rating_avg=4.0, rating_count=100,
        release_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_available_with_subscription=True, cached_at=now,
    ))
    s.add(BookGenre(book_id=book_id, genre_id="100", cached_at=now))
    s.add(BookAuthor(book_id=book_id, person_id=1, sort_order=0))
    s.add(BookNarrator(book_id=book_id, person_id=2))
    s.flush()


def _mock_session(db_session):
    """Create a mock SessionLocal context manager that returns db_session."""
    mock_factory = MagicMock()
    mock_factory.return_value.__enter__ = MagicMock(return_value=db_session)
    mock_factory.return_value.__exit__ = MagicMock(return_value=False)
    return mock_factory


def _make_app():
    """Create Flask test app."""
    from app.start import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def test_ignore_book_creates_row(db_session):
    """POST /ignore/<book_id> creates a UserIgnoredBook row."""
    _seed_base(db_session)
    _add_book(db_session, 1)
    db_session.commit()

    app = _make_app()
    mock_sf = _mock_session(db_session)
    with (
        patch("app.middleware.SessionLocal", mock_sf),
        patch("app.controllers.catalog.SessionLocal", mock_sf),
    ):
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            resp = c.post("/ignore/1")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["book_id"] == 1

    row = db_session.get(UserIgnoredBook, (1, 1))
    assert row is not None
    assert row.user_id == 1
    assert row.book_id == 1


def test_ignore_book_idempotent(db_session):
    """Ignoring the same book twice does not raise an error."""
    _seed_base(db_session)
    _add_book(db_session, 1)
    db_session.commit()

    app = _make_app()
    mock_sf = _mock_session(db_session)
    with (
        patch("app.middleware.SessionLocal", mock_sf),
        patch("app.controllers.catalog.SessionLocal", mock_sf),
    ):
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            c.post("/ignore/1")
            resp = c.post("/ignore/1")
            assert resp.status_code == 200


def test_unignore_book_deletes_row(db_session):
    """POST /unignore/<book_id> removes the UserIgnoredBook row."""
    _seed_base(db_session)
    _add_book(db_session, 1)
    db_session.add(UserIgnoredBook(user_id=1, book_id=1, ignored_at=datetime.now(timezone.utc)))
    db_session.commit()

    app = _make_app()
    mock_sf = _mock_session(db_session)
    with (
        patch("app.middleware.SessionLocal", mock_sf),
        patch("app.controllers.catalog.SessionLocal", mock_sf),
    ):
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            resp = c.post("/unignore/1")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True

    row = db_session.get(UserIgnoredBook, (1, 1))
    assert row is None


def test_ignored_book_excluded_from_catalog(db_session):
    """An ignored book does not appear in get_catalog() results."""
    _seed_base(db_session)
    _add_book(db_session, 1)
    _add_book(db_session, 2)
    db_session.add(UserIgnoredBook(user_id=1, book_id=1, ignored_at=datetime.now(timezone.utc)))
    db_session.flush()

    query = CatalogQuery(genre_id="100")
    result = get_catalog(db_session, query, user_id=1)

    book_ids = [card.book_id for card in result.cards if card.type == "book"]
    assert 1 not in book_ids
    assert 2 in book_ids


def test_ignore_series(db_session):
    """POST /ignore/series/<series_id> ignores all books in the series."""
    _seed_base(db_session)
    s = db_session
    s.add(Series(id=1, name="S1", slug="s1", url="/series/s1/", book_count=2))
    s.flush()
    _add_book(s, 10, title="Series Book")
    _add_book(s, 11, title="Series Book")
    s.add(BookSeries(book_id=10, series_id=1, position_in_series=1))
    s.add(BookSeries(book_id=11, series_id=1, position_in_series=2))
    s.commit()

    app = _make_app()
    mock_sf = _mock_session(db_session)
    with (
        patch("app.middleware.SessionLocal", mock_sf),
        patch("app.controllers.catalog.SessionLocal", mock_sf),
    ):
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            resp = c.post("/ignore/series/1")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["series_id"] == 1
            assert data["count"] == 2

    assert db_session.get(UserIgnoredBook, (1, 10)) is not None
    assert db_session.get(UserIgnoredBook, (1, 11)) is not None


def test_unignore_series(db_session):
    """POST /unignore/series/<series_id> unignores all books in the series."""
    _seed_base(db_session)
    s = db_session
    s.add(Series(id=1, name="S1", slug="s1", url="/series/s1/", book_count=2))
    s.flush()
    _add_book(s, 10)
    _add_book(s, 11)
    s.add(BookSeries(book_id=10, series_id=1, position_in_series=1))
    s.add(BookSeries(book_id=11, series_id=1, position_in_series=2))
    s.add(UserIgnoredBook(user_id=1, book_id=10, ignored_at=datetime.now(timezone.utc)))
    s.add(UserIgnoredBook(user_id=1, book_id=11, ignored_at=datetime.now(timezone.utc)))
    s.commit()

    app = _make_app()
    mock_sf = _mock_session(db_session)
    with (
        patch("app.middleware.SessionLocal", mock_sf),
        patch("app.controllers.catalog.SessionLocal", mock_sf),
    ):
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            resp = c.post("/unignore/series/1")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True

    assert db_session.get(UserIgnoredBook, (1, 10)) is None
    assert db_session.get(UserIgnoredBook, (1, 11)) is None


def test_clear_ignored(db_session):
    """POST /ignore/clear removes all ignore entries for the user."""
    _seed_base(db_session)
    _add_book(db_session, 1)
    _add_book(db_session, 2)
    _add_book(db_session, 3)
    db_session.add(UserIgnoredBook(user_id=1, book_id=1, ignored_at=datetime.now(timezone.utc)))
    db_session.add(UserIgnoredBook(user_id=1, book_id=2, ignored_at=datetime.now(timezone.utc)))
    db_session.add(UserIgnoredBook(user_id=1, book_id=3, ignored_at=datetime.now(timezone.utc)))
    db_session.commit()

    app = _make_app()
    mock_sf = _mock_session(db_session)
    with (
        patch("app.middleware.SessionLocal", mock_sf),
        patch("app.controllers.catalog.SessionLocal", mock_sf),
    ):
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            resp = c.post("/ignore/clear")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True

    count = db_session.query(UserIgnoredBook).filter(UserIgnoredBook.user_id == 1).count()
    assert count == 0
