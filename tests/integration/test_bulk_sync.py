"""Integration tests for run_bulk() against real LitRes.

These tests make real HTTP requests (max 2 pages + arts detail calls).
Run manually:
    pytest tests/integration/test_bulk_sync.py -v -s -m integration

Preconditions:
    - Network access to api.litres.ru and www.litres.ru

Genre seeding: loaded from tests/fixtures/genres.json (407 rows copied from
prod DB). No network call needed — genres are stable fixture data.
"""

import json
from pathlib import Path

import pytest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Book, BookGenre, BookSeries, Genre, Series, SyncConfig, SyncRun

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# LitRes Фантастика genre — used as sync target so catalog books have genre IDs
# that are covered by our fixture data.
_FANTASTIKA_GENRE_ID = 5004
_FANTASTIKA_GENRE_SLUG = "knigi-fantastika-5004"


_TEST_DB = Path(__file__).parent.parent / "litres_test.db"


@pytest.fixture
def sync_db():
    """File-based SQLite at tests/litres_test.db, recreated fresh at test start.

    DB is NOT deleted after the test — open it in SQLite Viewer to inspect results.
    Genres loaded from tests/fixtures/genres.json — no network call.
    Tests must patch app.sync.bulk.GENRE_ID to _FANTASTIKA_GENRE_ID.
    """
    if _TEST_DB.exists():
        _TEST_DB.unlink()
    engine = create_engine(
        f"sqlite:///{_TEST_DB}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    genres_data = json.loads((_FIXTURES_DIR / "genres.json").read_text(encoding="utf-8"))

    with Factory() as s:
        s.add(SyncConfig(
            genre_slug=_FANTASTIKA_GENRE_SLUG,
            genre_id=_FANTASTIKA_GENRE_ID,
            art_type="audiobook",
            language_code="ru",
            only_subscription=True,
        ))
        for g in genres_data:
            s.add(Genre(id=g["id"], parent_id=g["parent_id"], name=g["name"],
                        slug=g["slug"], url=g["url"]))
        s.commit()

    return Factory


@pytest.mark.integration
def test_run_bulk_two_pages(sync_db):
    """2-page run against Фантастика: correct sync_run state and data quality."""
    from app.sync.bulk import run_bulk

    with patch("app.sync.bulk.SessionLocal", sync_db), \
         patch("app.sync.bulk.GENRE_ID", _FANTASTIKA_GENRE_ID):
        run_bulk(resume=False, max_pages=2, dry_run=False, verbose=True)

    with sync_db() as s:
        run = s.query(SyncRun).filter_by(type="bulk").order_by(SyncRun.started_at.desc()).first()
        assert run is not None
        assert run.status == "done"
        assert run.pages_fetched == 2
        assert run.last_page_fetched == 1

        book_count = s.query(Book).count()
        assert book_count > 0, "Expected books after 2 pages"

        # Data quality: no missing required fields
        missing_release = s.query(Book).filter(Book.release_date.is_(None)).count()
        missing_cover = s.query(Book).filter(Book.cover_url.is_(None)).count()
        missing_rating = s.query(Book).filter(Book.rating_avg.is_(None)).count()
        assert missing_release == 0, f"{missing_release} books missing release_date"
        assert missing_cover == 0, f"{missing_cover} books missing cover_url"
        assert missing_rating == 0, f"{missing_rating} books missing rating_avg"

        # Genre coverage: ≥90% with full genre fixture loaded
        books_with_genre = s.query(BookGenre.book_id).distinct().count()
        coverage = books_with_genre / book_count if book_count else 0
        assert coverage >= 0.9, (
            f"Genre coverage too low: {books_with_genre}/{book_count} = {coverage:.0%}"
        )

        # No orphan series: every Series row must have ≥1 BookSeries link
        series_count = s.query(Series).count()
        if series_count > 0:
            orphan_series = (
                s.query(Series)
                .outerjoin(BookSeries, Series.id == BookSeries.series_id)
                .filter(BookSeries.series_id.is_(None))
                .count()
            )
            assert orphan_series == 0, (
                f"{orphan_series}/{series_count} series have no book_series links"
            )

