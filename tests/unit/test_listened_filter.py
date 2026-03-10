# tests/unit/test_listened_filter.py
"""Unit tests for F-5 (hide listened) and F-11 (incomplete series) filters."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Book, BookSeries, Person, BookAuthor, Series, User, UserListenedBook,
)
from app.models.catalog_query import CatalogQuery
from app.services.catalog_service import get_catalog


@pytest.fixture
def listened_db():
    """In-memory DB with books, series, and listened records for F-5/F-11 tests."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    now = datetime.now(timezone.utc)

    with Factory() as s:
        s.add(User(id=1, created_at=now))

        # Author
        s.add(Person(id=1, full_name="Author A", url="/a/1"))

        # Standalone books
        s.add(Book(id=1, title="Standalone Listened", url="/b/1", art_type=1, cached_at=now, release_date=now))
        s.add(Book(id=2, title="Standalone Not Listened", url="/b/2", art_type=1, cached_at=now, release_date=now))
        s.add(BookAuthor(book_id=1, person_id=1, sort_order=0))
        s.add(BookAuthor(book_id=2, person_id=1, sort_order=0))

        # Series A: fully listened (2 books, both listened)
        s.add(Series(id=10, name="Fully Listened Series", url="/s/10"))
        s.add(Book(id=10, title="SerA Book1", url="/b/10", art_type=1, cached_at=now, release_date=now))
        s.add(Book(id=11, title="SerA Book2", url="/b/11", art_type=1, cached_at=now, release_date=now))
        s.add(BookSeries(book_id=10, series_id=10, position_in_series=1))
        s.add(BookSeries(book_id=11, series_id=10, position_in_series=2))
        s.add(BookAuthor(book_id=10, person_id=1, sort_order=0))
        s.add(BookAuthor(book_id=11, person_id=1, sort_order=0))

        # Series B: partially listened (3 books, 2 listened, 1 not)
        s.add(Series(id=20, name="Partial Series", url="/s/20"))
        s.add(Book(id=20, title="SerB Book1", url="/b/20", art_type=1, cached_at=now, release_date=now))
        s.add(Book(id=21, title="SerB Book2", url="/b/21", art_type=1, cached_at=now, release_date=now))
        s.add(Book(id=22, title="SerB Book3", url="/b/22", art_type=1, cached_at=now, release_date=now))
        s.add(BookSeries(book_id=20, series_id=20, position_in_series=1))
        s.add(BookSeries(book_id=21, series_id=20, position_in_series=2))
        s.add(BookSeries(book_id=22, series_id=20, position_in_series=3))
        s.add(BookAuthor(book_id=20, person_id=1, sort_order=0))
        s.add(BookAuthor(book_id=21, person_id=1, sort_order=0))
        s.add(BookAuthor(book_id=22, person_id=1, sort_order=0))

        # Series C: not listened at all (2 books)
        s.add(Series(id=30, name="Not Listened Series", url="/s/30"))
        s.add(Book(id=30, title="SerC Book1", url="/b/30", art_type=1, cached_at=now, release_date=now))
        s.add(Book(id=31, title="SerC Book2", url="/b/31", art_type=1, cached_at=now, release_date=now))
        s.add(BookSeries(book_id=30, series_id=30, position_in_series=1))
        s.add(BookSeries(book_id=31, series_id=30, position_in_series=2))
        s.add(BookAuthor(book_id=30, person_id=1, sort_order=0))
        s.add(BookAuthor(book_id=31, person_id=1, sort_order=0))

        # Listened records
        s.add(UserListenedBook(user_id=1, book_id=1, listened_at=now))   # standalone
        s.add(UserListenedBook(user_id=1, book_id=10, listened_at=now))  # series A, book 1
        s.add(UserListenedBook(user_id=1, book_id=11, listened_at=now))  # series A, book 2
        s.add(UserListenedBook(user_id=1, book_id=20, listened_at=now))  # series B, book 1
        s.add(UserListenedBook(user_id=1, book_id=21, listened_at=now))  # series B, book 2

        s.commit()
    return Factory


class TestF5HideListened:
    def test_no_filter_returns_all(self, listened_db):
        """Without hide_listened, all cards are returned."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery())
            # 2 standalone + 3 series = 5 cards
            assert result.total_count == 5

    def test_hide_listened_removes_listened_standalone(self, listened_db):
        """F-5 hides standalone books that are listened."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(hide_listened=True))
            titles = [c.title for c in result.cards]
            assert "Standalone Listened" not in titles
            assert "Standalone Not Listened" in titles

    def test_hide_listened_removes_fully_listened_series(self, listened_db):
        """F-5 hides series where ALL books are listened."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(hide_listened=True))
            titles = [c.title for c in result.cards]
            assert "Fully Listened Series" not in titles

    def test_hide_listened_keeps_partially_listened_series(self, listened_db):
        """F-5 keeps series where at least one book is NOT listened."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(hide_listened=True))
            titles = [c.title for c in result.cards]
            assert "Partial Series" in titles

    def test_hide_listened_keeps_not_listened_series(self, listened_db):
        """F-5 keeps series where no books are listened."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(hide_listened=True))
            titles = [c.title for c in result.cards]
            assert "Not Listened Series" in titles


class TestF11IncompleteSeries:
    def test_incomplete_series_only(self, listened_db):
        """F-11 shows only series with >=1 listened AND >=1 unlistened book."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(incomplete_series_only=True))
            titles = [c.title for c in result.cards]
            # Only Partial Series qualifies
            assert titles == ["Partial Series"]

    def test_incomplete_excludes_fully_listened(self, listened_db):
        """F-11 excludes fully listened series."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(incomplete_series_only=True))
            titles = [c.title for c in result.cards]
            assert "Fully Listened Series" not in titles

    def test_incomplete_excludes_not_started(self, listened_db):
        """F-11 excludes series with zero listened books."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(incomplete_series_only=True))
            titles = [c.title for c in result.cards]
            assert "Not Listened Series" not in titles

    def test_incomplete_excludes_standalones(self, listened_db):
        """F-11 excludes standalone books entirely."""
        with listened_db() as session:
            result = get_catalog(session, CatalogQuery(incomplete_series_only=True))
            titles = [c.title for c in result.cards]
            assert "Standalone Listened" not in titles
            assert "Standalone Not Listened" not in titles
