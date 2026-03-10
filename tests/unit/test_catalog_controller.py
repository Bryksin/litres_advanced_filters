"""Smoke test for catalog controller — renders without errors."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.db.models import Genre, User, UserSettings
from app.services.cards import BookCard
from app.services.catalog_service import CatalogResult


def test_index_renders_200(db_session):
    """GET / returns 200 with mocked service layer."""
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(user_id=1))
    db_session.add(Genre(id="5077", name="Test", slug="test", url="/genre/test/"))
    db_session.flush()

    mock_result = CatalogResult(
        cards=[BookCard(
            type="book",
            title="Test Book",
            cover_url=None,
            url="https://www.litres.ru/book/1",
            rating_avg=4.0,
            rating_count=100,
            book_id=1,
            authors=["Test Author"],
        )],
        total_count=1,
        page=1,
        page_size=48,
        has_next=False,
    )

    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__ = MagicMock(return_value=db_session)
    mock_session_factory.return_value.__exit__ = MagicMock(return_value=False)

    with (
        patch("app.middleware.SessionLocal", mock_session_factory),
        patch("app.controllers.catalog.SessionLocal", mock_session_factory),
        patch("app.controllers.auth.SessionLocal", mock_session_factory),
        patch("app.controllers.catalog.get_catalog", return_value=mock_result),
        patch("app.controllers.catalog.get_genre_tree", return_value=[]),
        patch("app.controllers.catalog.get_genre_ancestor_ids", return_value={"5077"}),
    ):
        with app.test_client() as c:
            # The middleware will auto-create a user OR find user_id=1
            # Pre-set session to use our seeded user
            with c.session_transaction() as sess:
                sess["user_id"] = 1
            resp = c.get("/")
            assert resp.status_code == 200
