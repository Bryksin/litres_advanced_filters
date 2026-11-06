"""Unit tests for heal command (BUG-1 narrators + BUG-2 genres)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.db.models import Book, BookGenre, BookNarrator, Genre
from app.scrapers.models import (
    Art,
    ArtPrices,
    ArtRating,
    PersonRef,
    ArtDetail,
    ArtGenreRef,
)


def _seed_book(session, book_id: int) -> Book:
    """Insert a book row with no narrator or genre links."""
    book = Book(
        id=book_id,
        title=f"Test Book {book_id}",
        url=f"/audiobook/test-{book_id}/",
        art_type=1,
        rating_avg=4.0,
        rating_count=50,
        is_available_with_subscription=True,
        is_abonement_art=True,
        cached_at=datetime.now(timezone.utc),
    )
    session.add(book)
    session.flush()
    return book


def _make_detail(art_id: int, persons: list[PersonRef] | None = None,
                 genres: list[ArtGenreRef] | None = None) -> ArtDetail:
    art = Art(
        id=art_id,
        title=f"Test {art_id}",
        url=f"/audiobook/test-{art_id}/",
        art_type=1,
        persons=persons or [],
        rating=ArtRating(rated_avg=4.0, rated_total_count=50),
        prices=ArtPrices(final_price=0.0, full_price=0.0, currency="RUB"),
        is_available_with_subscription=True,
        is_abonement_art=True,
    )
    return ArtDetail(art=art, genres=genres or [], series=[], release_date=None)


# ---- heal_narrators tests ----


def test_heal_narrators_reader_role(db_session):
    """Heal finds narrator with role='reader' and creates BookNarrator."""
    from app.sync.heal import heal_narrators

    _seed_book(db_session, 200)
    detail = _make_detail(200, persons=[
        PersonRef(id=10, full_name="Author", url="/a/", role="author"),
        PersonRef(id=20, full_name="Narrator", url="/n/", role="reader"),
    ])

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_narrators(db_session, client)

    assert stats["healed"] == 1
    assert db_session.get(BookNarrator, {"book_id": 200, "person_id": 20}) is not None


def test_heal_narrators_narrator_role(db_session):
    """Heal handles role='narrator' (AI auto-reader like Литрес Авточтец)."""
    from app.sync.heal import heal_narrators

    _seed_book(db_session, 201)
    detail = _make_detail(201, persons=[
        PersonRef(id=10, full_name="Author", url="/a/", role="author"),
        PersonRef(id=30, full_name="Литрес Авточтец", url="/avtochtec/", role="narrator"),
    ])

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_narrators(db_session, client)

    assert stats["healed"] == 1
    assert db_session.get(BookNarrator, {"book_id": 201, "person_id": 30}) is not None


def test_heal_narrators_skips_unresolvable(db_session):
    """Heal skips books where arts detail has no narrator."""
    from app.sync.heal import heal_narrators

    _seed_book(db_session, 202)
    detail = _make_detail(202, persons=[
        PersonRef(id=10, full_name="Author", url="/a/", role="author"),
    ])

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_narrators(db_session, client)

    assert stats["healed"] == 0
    assert stats["unresolvable"] == 1


def test_heal_narrators_handles_api_failure(db_session):
    """Heal continues on API failure for individual books."""
    from app.sync.heal import heal_narrators

    _seed_book(db_session, 203)

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", side_effect=Exception("API error")):
        stats = heal_narrators(db_session, client)

    assert stats["healed"] == 0
    assert stats["failed"] == 1


# ---- heal_genres tests ----


def test_heal_genres_adds_missing_links(db_session):
    """Heal finds genre-less book, re-fetches, and adds BookGenre links."""
    from app.sync.heal import heal_genres

    _seed_book(db_session, 300)
    # Seed a genre in the DB so FK guard passes
    db_session.add(Genre(id="5003", name="Фантастика", slug="fantastika-5003",
                         url="/genre/fantastika-5003/", count=100))
    db_session.flush()

    detail = _make_detail(300, genres=[
        ArtGenreRef(id=5003, name="Фантастика", url="/genre/fantastika-5003/"),
    ])

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_genres(db_session, client)

    assert stats["healed"] == 1
    assert db_session.get(BookGenre, {"book_id": 300, "genre_id": "5003"}) is not None


def test_heal_genres_auto_inserts_unknown_genre(db_session):
    """Heal auto-inserts unknown genres with a name and creates BookGenre link."""
    from app.sync.heal import heal_genres

    _seed_book(db_session, 301)
    detail = _make_detail(301, genres=[
        ArtGenreRef(id=9999, name="Unknown", url="/genre/unknown-9999/"),
    ])

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_genres(db_session, client)

    assert stats["healed"] == 1
    assert db_session.get(Genre, "9999") is not None
    assert db_session.get(BookGenre, {"book_id": 301, "genre_id": "9999"}) is not None


def test_heal_genres_skips_nameless_genre(db_session):
    """Heal skips genres with no name (e.g. deleted genre IDs)."""
    from app.sync.heal import heal_genres

    _seed_book(db_session, 303)
    detail = _make_detail(303, genres=[
        ArtGenreRef(id=8888, name="", url=""),
    ])

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_genres(db_session, client)

    assert stats["healed"] == 0
    assert stats["still_empty"] == 1
    assert db_session.get(Genre, "8888") is None


def test_heal_genres_handles_api_failure(db_session):
    """Heal continues on API failure."""
    from app.sync.heal import heal_genres

    _seed_book(db_session, 302)

    client = MagicMock()
    with patch("app.sync.heal.fetch_arts_detail", side_effect=Exception("API error")):
        stats = heal_genres(db_session, client)

    assert stats["healed"] == 0
    assert stats["failed"] == 1


# --- S12: per-book savepoint + heal_authors -------------------------------


def test_heal_narrators_db_error_does_not_poison_later_books(db_session):
    """One IntegrityError must not roll back or block the other books (S12)."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    from app.db.models import Book, BookNarrator, Person
    from app.scrapers.models import Art, ArtDetail, ArtPrices, ArtRating, PersonRef
    from app.sync.heal import heal_narrators

    now = datetime.now(timezone.utc)
    for bid in (1, 2, 3):
        db_session.add(Book(id=bid, title="t", url="/u/", art_type=1, cached_at=now))
    db_session.commit()

    def detail(client, book_id):
        art = Art(id=book_id, title="t", url="/u/", art_type=1,
                  persons=[PersonRef(id=500 + book_id, full_name="N", url="/n/", role="reader")],
                  rating=ArtRating(None, 0), prices=ArtPrices(0.0, 0.0, "RUB"),
                  is_available_with_subscription=True, is_abonement_art=True)
        return ArtDetail(art=art, genres=[], series=[], release_date=None)

    real_add = db_session.add

    def add(obj):
        real_add(obj)
        if isinstance(obj, Person) and obj.id == 502:
            real_add(Person(id=501, full_name="dup", url=""))  # duplicate PK -> IntegrityError

    with patch("app.sync.heal.fetch_arts_detail", side_effect=detail), \
         patch.object(db_session, "add", side_effect=add):
        stats = heal_narrators(db_session, MagicMock())
    db_session.commit()

    assert stats["failed"] == 1 and stats["healed"] == 2
    assert {n.book_id for n in db_session.query(BookNarrator)} == {1, 3}


def test_heal_authors_backfills(db_session):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    from app.db.models import Book, BookAuthor
    from app.scrapers.models import Art, ArtDetail, ArtPrices, ArtRating, PersonRef
    from app.sync.heal import heal_authors

    db_session.add(Book(id=7, title="t", url="/u/", art_type=1, cached_at=datetime.now(timezone.utc)))
    db_session.commit()
    art = Art(id=7, title="t", url="/u/", art_type=1,
              persons=[PersonRef(id=70, full_name="A", url=None, role="author"),
                       PersonRef(id=71, full_name="R", url="/r/", role="reader")],
              rating=ArtRating(None, 0), prices=ArtPrices(0.0, 0.0, "RUB"),
              is_available_with_subscription=True, is_abonement_art=True)
    detail = ArtDetail(art=art, genres=[], series=[], release_date=None)
    with patch("app.sync.heal.fetch_arts_detail", return_value=detail):
        stats = heal_authors(db_session, MagicMock())
    assert stats["healed"] == 1
    assert [a.person_id for a in db_session.query(BookAuthor).filter_by(book_id=7)] == [70]
