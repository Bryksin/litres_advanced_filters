"""Catalog controller: query parameters reaching get_catalog() (Phase 16)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import Genre, User, UserSettings
from app.services.catalog_service import CatalogResult


@pytest.fixture
def call_index(db_session):
    """Return a function that GETs / and returns the CatalogQuery passed to get_catalog."""
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(Genre(id="5077", name="Space", slug="space", url="/genre/space/"))
    db_session.add(Genre(id="201583", name="Root", slug="root", url="/genre/root/"))
    db_session.flush()

    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True

    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=db_session)
    factory.return_value.__exit__ = MagicMock(return_value=False)

    def _call(url, *, authenticated=False):
        captured = {}

        def fake_get_catalog(session, query, user_id=1):
            captured["query"] = query
            return CatalogResult(cards=[], total_count=0, page=query.page,
                                 page_size=query.page_size, has_next=False)

        with (
            patch("app.middleware.SessionLocal", factory),
            patch("app.controllers.catalog.SessionLocal", factory),
            patch("app.controllers.auth.SessionLocal", factory),
            patch("app.controllers.catalog.get_catalog", side_effect=fake_get_catalog),
            patch("app.controllers.catalog.get_genre_tree", return_value=[]),
            patch("app.controllers.auth._is_authenticated",
                  return_value=(authenticated, "u@example.com" if authenticated else None)),
            patch("app.controllers.auth._get_listened_count", return_value=0),
        ):
            with app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["user_id"] = 1
                resp = c.get(url)
        assert resp.status_code == 200
        return captured["query"]

    return _call


@pytest.mark.parametrize("raw, expected", [("abc", 1), ("0", 1), ("-3", 1), ("2", 2)])
def test_page_param_is_sanitised(call_index, raw, expected):
    """A malformed or non-positive page no longer crashes or yields a negative OFFSET."""
    assert call_index(f"/?page={raw}").page == expected


def test_saved_hide_listened_not_applied_when_logged_out(call_index, db_session):
    """Saved hide_listened must not filter by a stale list after logout (non-submit load)."""
    db_session.add(UserSettings(user_id=1, genre_id="5077", hide_listened=True))
    db_session.flush()
    assert call_index("/", authenticated=False).hide_listened is False
    assert call_index("/", authenticated=True).hide_listened is True


def test_no_saved_genre_filters_by_highlighted_default(call_index):
    """With no saved genre the catalog uses the same default genre the sidebar highlights."""
    from app.config.config import Config

    assert call_index("/").genre_id == Config.DEFAULT_GENRE_ID
