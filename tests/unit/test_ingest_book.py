"""Unit tests for ingest_book(). Uses in-memory SQLite via db_session fixture.

RateLimitedClient is never called in these tests — arts_detail is passed directly.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.db.models import Book, BookAuthor, BookGenre, BookNarrator, Person, Genre
from app.scrapers.models import Art, ArtPrices, ArtRating, PersonRef
from app.scrapers.arts import ArtDetail
from app.scrapers.models import ArtGenreRef, ArtSeriesRef


def _make_art(art_id: int = 100, title: str = "Test Book") -> Art:
    return Art(
        id=art_id,
        title=title,
        url=f"/audiobook/author/test-{art_id}/",
        art_type=1,
        persons=[
            PersonRef(id=1, full_name="Author One", url="/author/one/", role="author"),
            PersonRef(id=2, full_name="Narrator Two", url="/author/two/", role="reader"),
        ],
        rating=ArtRating(rated_avg=4.5, rated_total_count=100),
        prices=ArtPrices(final_price=0.0, full_price=0.0, currency="RUB"),
        is_available_with_subscription=True,
        is_abonement_art=True,
        language_code="ru",
        cover_url="https://www.litres.ru/pub/c/cover/100.jpg",
    )


def _make_detail(art: Art, genre_id: str = "5077") -> ArtDetail:
    return ArtDetail(
        art=art,
        genres=[ArtGenreRef(id=int(genre_id), name="космическая фантастика", url=f"/genre/kosmicheskaya-{genre_id}/")],
        series=[],
        release_date=datetime(2025, 10, 3, tzinfo=timezone.utc),
    )


def _seed_genre(session, genre_id: str = "5077") -> None:
    session.add(Genre(id=genre_id, name="Test Genre", slug=f"test-{genre_id}", url=f"/genre/test-{genre_id}/"))
    session.flush()


# --- Tests ---


def test_ingest_book_new_creates_book_row(db_session):
    """New book: Book row created with correct fields."""
    from app.sync.ingest import ingest_book

    _seed_genre(db_session)
    art = _make_art(art_id=100)
    detail = _make_detail(art)

    is_new, _ = ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)

    assert is_new is True
    book = db_session.get(Book, 100)
    assert book is not None
    assert book.title == "Test Book"
    assert book.rating_avg == 4.5
    assert book.rating_count == 100
    assert book.is_available_with_subscription is True
    # SQLite DateTime drops tzinfo on round-trip; compare naive UTC datetimes
    assert book.release_date == datetime(2025, 10, 3)


def test_ingest_book_new_creates_persons_and_junctions(db_session):
    """New book: Person, BookAuthor, BookNarrator rows created."""
    from app.sync.ingest import ingest_book

    _seed_genre(db_session)
    art = _make_art(art_id=101)
    detail = _make_detail(art)

    ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)  # noqa: result unused

    assert db_session.get(Person, 1) is not None
    assert db_session.get(Person, 2) is not None
    ba = db_session.get(BookAuthor, {"book_id": 101, "person_id": 1})
    assert ba is not None
    bn = db_session.get(BookNarrator, {"book_id": 101, "person_id": 2})
    assert bn is not None


def test_ingest_book_new_creates_book_genre(db_session):
    """New book: BookGenre junction created for known genre IDs."""
    from app.sync.ingest import ingest_book

    _seed_genre(db_session, "5077")
    art = _make_art(art_id=102)
    detail = _make_detail(art, genre_id="5077")

    ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)

    bg = db_session.get(BookGenre, {"book_id": 102, "genre_id": "5077"})
    assert bg is not None


def test_ingest_book_auto_inserts_unknown_genre(db_session):
    """New book: unknown genre with a name is auto-inserted; BookGenre link created."""
    from app.sync.ingest import ingest_book

    # No genre seeded — genre table is empty
    art = _make_art(art_id=103)
    detail = _make_detail(art, genre_id="9999")

    ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)

    # Genre auto-inserted, BookGenre link created
    assert db_session.get(Book, 103) is not None
    assert db_session.get(Genre, "9999") is not None
    bg = db_session.get(BookGenre, {"book_id": 103, "genre_id": "9999"})
    assert bg is not None


def test_ingest_book_skips_nameless_genre(db_session):
    """New book: genre with empty name is skipped (not inserted)."""
    from app.sync.ingest import ingest_book

    art = _make_art(art_id=104)
    detail = ArtDetail(
        art=art,
        genres=[ArtGenreRef(id=8888, name="", url="")],
        series=[],
        release_date=datetime(2025, 10, 3, tzinfo=timezone.utc),
    )

    ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)

    assert db_session.get(Genre, "8888") is None
    assert db_session.query(BookGenre).filter_by(book_id=104).count() == 0


def test_ingest_book_known_updates_rating_only(db_session):
    """Known book: only rating_avg and rating_count updated; no new rows created."""
    from app.sync.ingest import ingest_book

    # Pre-insert book
    db_session.add(Book(
        id=104, title="Old Title", url="/audiobook/old/",
        art_type=1, rating_avg=3.0, rating_count=10,
        is_available_with_subscription=True, is_abonement_art=False,
        cached_at=datetime.now(timezone.utc),
    ))
    db_session.flush()

    art = _make_art(art_id=104, title="New Title")
    art.rating.rated_avg = 4.9
    art.rating.rated_total_count = 999

    is_new, detail = ingest_book(db_session, client=MagicMock(), art=art, arts_detail=None)

    assert is_new is False
    assert detail is None
    book = db_session.get(Book, 104)
    assert book.title == "Old Title"   # title NOT updated
    assert book.rating_avg == 4.9     # rating updated
    assert book.rating_count == 999


def test_ingest_book_dry_run_new_no_db_write(db_session):
    """dry_run=True: new book detected but no rows written."""
    from app.sync.ingest import ingest_book

    art = _make_art(art_id=105)
    is_new, detail = ingest_book(db_session, client=MagicMock(), art=art, arts_detail=None, dry_run=True)

    assert is_new is True
    assert detail is None
    assert db_session.get(Book, 105) is None


def test_ingest_book_person_url_null_safety(db_session):
    """Person with url=None is stored as empty string (null-safety pattern)."""
    from app.sync.ingest import ingest_book

    art = _make_art(art_id=106)
    art.persons[0].url = None  # simulate null URL from series page
    detail = _make_detail(art)

    ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)

    p = db_session.get(Person, 1)
    assert p is not None
    assert p.url == ""


def test_ingest_book_narrator_role_creates_book_narrator(db_session):
    """New book with role='narrator' (AI auto-reader): BookNarrator row created."""
    from app.sync.ingest import ingest_book

    _seed_genre(db_session)
    art = _make_art(art_id=110)
    art.persons = [
        PersonRef(id=1, full_name="Author One", url="/author/one/", role="author"),
        PersonRef(id=3, full_name="Литрес Авточтец", url="/author/avtochtec-litres/", role="narrator"),
    ]
    detail = _make_detail(art)

    ingest_book(db_session, client=MagicMock(), art=art, arts_detail=detail)

    bn = db_session.get(BookNarrator, {"book_id": 110, "person_id": 3})
    assert bn is not None
    p = db_session.get(Person, 3)
    assert p.full_name == "Литрес Авточтец"
