"""Unit tests for ingest_series(). Mocks fetch_series_page and fetch_arts_detail."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import Book, BookSeries, Series
from app.scrapers.models import Art, ArtPrices, ArtRating, PersonRef, SeriesBookEntry, SeriesPage
from app.scrapers.arts import ArtDetail
from app.scrapers.models import ArtGenreRef


def _make_series_page(series_id: int, members: list[int]) -> SeriesPage:
    books = [
        SeriesBookEntry(
            art_id=art_id,
            title=f"Book {art_id}",
            url=f"/audiobook/author/book-{art_id}/",
            position=i + 1,
            authors=["Author"],
            narrators=[],
            rating_avg=4.0,
            rating_count=50,
            is_available_with_subscription=True,
            price_str=None,
        )
        for i, art_id in enumerate(members)
    ]
    return SeriesPage(series_id=series_id, series_name="Test Series",
                      series_url=f"/series/test-{series_id}/", books=books)


def _make_art_detail(art_id: int) -> ArtDetail:
    art = Art(
        id=art_id, title=f"Book {art_id}",
        url=f"/audiobook/author/book-{art_id}/",
        art_type=1,
        persons=[PersonRef(id=10 + art_id, full_name="Author", url="/author/a/", role="author")],
        rating=ArtRating(rated_avg=4.0, rated_total_count=50),
        prices=ArtPrices(final_price=0.0, full_price=0.0, currency="RUB"),
        is_available_with_subscription=True,
        is_abonement_art=False,
        language_code="ru",
    )
    return ArtDetail(art=art, genres=[], series=[], release_date=datetime(2025, 1, 1, tzinfo=timezone.utc))


def test_ingest_series_creates_series_and_links(db_session):
    """ingest_series creates Series row and BookSeries links for all members."""
    from app.sync.ingest import ingest_series

    series_page = _make_series_page(series_id=500, members=[201, 202])
    mock_client = MagicMock()

    with patch("app.sync.ingest.fetch_series_page", return_value=series_page), \
         patch("app.sync.ingest.fetch_arts_detail", side_effect=lambda c, art_id: _make_art_detail(art_id)):

        count = ingest_series(db_session, mock_client, 500, "Test Series",
                              "/series/test-500/", book_count=2)

    assert count == 2
    series = db_session.get(Series, 500)
    assert series is not None
    assert series.name == "Test Series"
    assert series.book_count == 2

    for art_id, pos in [(201, 1), (202, 2)]:
        bs = db_session.get(BookSeries, {"book_id": art_id, "series_id": 500})
        assert bs is not None
        assert bs.position_in_series == pos


def test_ingest_series_already_ingested_book_gets_link(db_session):
    """If book already in DB, ingest_series still creates the BookSeries link."""
    from app.sync.ingest import ingest_series
    from app.db.models import Book

    # Pre-insert book 201
    db_session.add(Book(
        id=201, title="Existing", url="/audiobook/a/",
        art_type=1, rating_avg=4.0, rating_count=10,
        is_available_with_subscription=True, is_abonement_art=False,
        cached_at=datetime.now(timezone.utc),
    ))
    db_session.flush()

    series_page = _make_series_page(series_id=501, members=[201])
    mock_client = MagicMock()

    with patch("app.sync.ingest.fetch_series_page", return_value=series_page), \
         patch("app.sync.ingest.fetch_arts_detail", side_effect=lambda c, art_id: _make_art_detail(art_id)):

        ingest_series(db_session, mock_client, 501, "S2", "/series/test-501/", book_count=1)

    bs = db_session.get(BookSeries, {"book_id": 201, "series_id": 501})
    assert bs is not None


def test_ingest_series_failed_member_continues(db_session):
    """One failing series member should not abort ingest of remaining members."""
    from app.sync.ingest import ingest_series

    series_page = _make_series_page(series_id=502, members=[301, 302])
    mock_client = MagicMock()

    def fail_for_301(client, art_id):
        if art_id == 301:
            raise RuntimeError("API error")
        return _make_art_detail(art_id)

    with patch("app.sync.ingest.fetch_series_page", return_value=series_page), \
         patch("app.sync.ingest.fetch_arts_detail", side_effect=fail_for_301):

        count = ingest_series(db_session, mock_client, 502, "S3", "/series/test-502/", book_count=2)

    # 301 failed, 302 succeeded
    assert count == 1
    assert db_session.get(Book, 302) is not None
    assert db_session.get(BookSeries, {"book_id": 302, "series_id": 502}) is not None


def test_ingest_series_dry_run(db_session):
    """dry_run=True returns 0 and writes nothing."""
    from app.sync.ingest import ingest_series

    count = ingest_series(db_session, MagicMock(), 503, "S4", "/series/test-503/", book_count=5, dry_run=True)

    assert count == 0
    assert db_session.get(Series, 503) is None
