"""Tests for auto-inserting unknown genres during ingest and heal."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession

from app.db.base import Base
from app.db.models import Book, BookGenre, Genre
from app.scrapers.models import (
    Art,
    ArtDetail,
    ArtGenreRef,
    ArtPrices,
    ArtRating,
    PersonRef,
)
from app.sync.ingest import ingest_book


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with SASession(engine) as s:
        s.add(Genre(id="100", name="Known Genre", slug="known-genre-100", url="/genre/known-genre-100/"))
        s.commit()
        yield s


def _make_art(art_id: int = 999) -> Art:
    return Art(
        id=art_id,
        title="Test Book",
        url=f"/audiobook/test-{art_id}/",
        art_type=1,
        cover_url="https://example.com/cover.jpg",
        language_code="ru",
        rating=ArtRating(rated_avg=4.0, rated_total_count=10),
        prices=ArtPrices(final_price=0, full_price=0, currency="RUB"),
        is_available_with_subscription=True,
        is_abonement_art=False,
        persons=[PersonRef(id=1, full_name="Author", role="author", url="/a/1/")],
    )


def _make_detail(art: Art, genres: list[ArtGenreRef]) -> ArtDetail:
    return ArtDetail(art=art, genres=genres, series=[], release_date=None)


class TestIngestGenreAutoInsert:
    def test_unknown_genre_auto_inserted(self, session: SASession):
        """When arts_detail has a genre not in DB, ingest should create it."""
        art = _make_art()
        genres = [
            ArtGenreRef(id=100, name="Known Genre", url="/genre/known-genre-100/"),
            ArtGenreRef(id=5296, name="классическая проза", url="/genre/klassicheskaya-proza-5296/"),
        ]
        detail = _make_detail(art, genres)

        ingest_book(session, MagicMock(), art, arts_detail=detail)

        genre = session.get(Genre, "5296")
        assert genre is not None
        assert genre.name == "классическая проза"

        bg1 = session.get(BookGenre, {"book_id": 999, "genre_id": "100"})
        bg2 = session.get(BookGenre, {"book_id": 999, "genre_id": "5296"})
        assert bg1 is not None
        assert bg2 is not None

    def test_nameless_genre_skipped(self, session: SASession):
        """Genre with empty name should be skipped, not inserted."""
        art = _make_art(998)
        genres = [ArtGenreRef(id=9999, name="", url="")]
        detail = _make_detail(art, genres)

        ingest_book(session, MagicMock(), art, arts_detail=detail)

        assert session.get(Genre, "9999") is None
        assert session.query(BookGenre).filter_by(book_id=998).count() == 0


class TestHealGenreAutoInsert:
    def test_heal_auto_inserts_unknown_genre(self, session: SASession):
        """heal_genres should auto-insert unknown genres instead of skipping."""
        from app.sync.heal import heal_genres

        book = Book(
            id=3001, title="Heal Genre Target", url="/audiobook/heal-genre/", art_type=1,
            cover_url="https://example.com/c.jpg", language_code="ru",
            rating_avg=4.0, rating_count=10,
            is_available_with_subscription=True, is_abonement_art=False,
            cached_at=datetime.now(timezone.utc),
        )
        session.add(book)
        session.commit()

        mock_art = _make_art(3001)
        mock_detail = ArtDetail(
            art=mock_art,
            genres=[ArtGenreRef(id=5297, name="русская классика", url="/genre/russkaya-klassika-5297/")],
            series=[],
            release_date=None,
        )

        client = MagicMock()
        with patch("app.sync.heal.fetch_arts_detail", return_value=mock_detail):
            stats = heal_genres(session, client)

        assert stats["healed"] == 1
        genre = session.get(Genre, "5297")
        assert genre is not None
        assert genre.name == "русская классика"

        bg = session.get(BookGenre, {"book_id": 3001, "genre_id": "5297"})
        assert bg is not None
