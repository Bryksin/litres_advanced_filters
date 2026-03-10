"""Tests for duplicate person dedup in ingest_book() and heal_narrators()."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession

from app.db.base import Base
from app.db.models import Book, BookAuthor, BookNarrator, Genre
from app.scrapers.models import (
    ArtDetail,
    ArtGenreRef,
    ArtRating,
    ArtPrices,
    Art,
    PersonRef,
)
from app.sync.ingest import ingest_book


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with SASession(engine) as s:
        # Seed a genre so BookGenre FK doesn't block
        s.add(Genre(id="100", name="Test Genre", slug="test-genre-100", url="/genre/test-genre-100/"))
        s.commit()
        yield s


def _make_art(art_id: int, persons: list[PersonRef]) -> Art:
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
        persons=persons,
    )


def _make_detail(art: Art) -> ArtDetail:
    return ArtDetail(
        art=art,
        genres=[ArtGenreRef(id=100, name="Test Genre", url="/genre/test-genre-100/")],
        series=[],
        release_date=None,
    )


class TestDuplicateNarratorDedup:
    """LitRes can return the same person with both 'narrator' and 'reader' roles."""

    def test_ingest_book_deduplicates_narrators(self, session: SASession):
        """Same person_id with role='narrator' and role='reader' should produce one BookNarrator row."""
        persons = [
            PersonRef(id=100, full_name="Author One", role="author", url="/a/1/"),
            PersonRef(id=200, full_name="AI Reader", role="narrator", url="/a/2/"),
            PersonRef(id=200, full_name="AI Reader", role="reader", url="/a/2/"),
        ]
        art = _make_art(1001, persons)
        detail = _make_detail(art)

        is_new, _ = ingest_book(session, MagicMock(), art, arts_detail=detail)

        assert is_new is True
        narrators = session.query(BookNarrator).filter_by(book_id=1001).all()
        assert len(narrators) == 1
        assert narrators[0].person_id == 200

    def test_ingest_book_deduplicates_authors(self, session: SASession):
        """Same person_id appearing twice as 'author' should produce one BookAuthor row."""
        persons = [
            PersonRef(id=100, full_name="Author One", role="author", url="/a/1/"),
            PersonRef(id=100, full_name="Author One", role="author", url="/a/1/"),
        ]
        art = _make_art(1002, persons)
        detail = _make_detail(art)

        is_new, _ = ingest_book(session, MagicMock(), art, arts_detail=detail)

        assert is_new is True
        authors = session.query(BookAuthor).filter_by(book_id=1002).all()
        assert len(authors) == 1
        assert authors[0].person_id == 100


class TestHealNarratorDedup:
    """heal_narrators should also dedup same-person dual-role narrators."""

    def test_heal_deduplicates_narrators(self, session: SASession):
        """heal_narrators with a book whose API returns duplicate narrator roles."""
        from app.sync.heal import heal_narrators

        # Create a book with no narrators (heal target)
        book = Book(
            id=2001, title="Heal Target", url="/audiobook/heal/", art_type=1,
            cover_url="https://example.com/c.jpg", language_code="ru",
            rating_avg=4.0, rating_count=10,
            is_available_with_subscription=True, is_abonement_art=False,
            cached_at=datetime.now(timezone.utc),
        )
        session.add(book)
        session.commit()

        # Mock arts detail returning duplicate narrator roles
        mock_art = _make_art(2001, [
            PersonRef(id=300, full_name="Dual Reader", role="narrator", url="/a/3/"),
            PersonRef(id=300, full_name="Dual Reader", role="reader", url="/a/3/"),
        ])
        mock_detail = _make_detail(mock_art)

        client = MagicMock()
        with patch("app.sync.heal.fetch_arts_detail", return_value=mock_detail):
            stats = heal_narrators(session, client)

        assert stats["healed"] == 1
        narrators = session.query(BookNarrator).filter_by(book_id=2001).all()
        assert len(narrators) == 1
        assert narrators[0].person_id == 300
