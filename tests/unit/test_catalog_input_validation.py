"""Catalog request validation (Phase 16 audit Q2-Q5, F6, F7, Q-S5, Q-S6, Q9).

These go through the real controller and the real get_catalog() so a value
that would crash the SQL layer shows up as a non-200 response.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import Book, BookGenre, Genre, User, UserSettings
from app.models.catalog_query import CatalogQuery
from app.services.settings_service import SettingsService


@pytest.fixture
def client_get(db_session):
    """GET a URL through the app; returns (status_code, saved UserSettings row)."""
    now = datetime.now(timezone.utc)
    db_session.add(User(id=1, created_at=now))
    db_session.add(Genre(id="5077", name="Space", slug="space", url="/genre/space/"))
    db_session.add(Genre(id="201583", name="Root", slug="root", url="/genre/root/"))
    db_session.add(UserSettings(user_id=1, genre_id="5077"))
    db_session.add(Book(id=1, title="B", url="/audiobook/1/", art_type=1, rating_avg=4.0,
                        rating_count=3, release_date=now, is_available_with_subscription=True,
                        cached_at=now))
    db_session.add(BookGenre(book_id=1, genre_id="5077", cached_at=now))
    db_session.flush()

    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True
    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=db_session)
    factory.return_value.__exit__ = MagicMock(return_value=False)

    def _get(url, *, authenticated=False):
        with (
            patch("app.middleware.SessionLocal", factory),
            patch("app.controllers.catalog.SessionLocal", factory),
            patch("app.controllers.auth.SessionLocal", factory),
            patch("app.controllers.catalog.get_genre_tree", return_value=[]),
            patch("app.controllers.auth._is_authenticated",
                  return_value=(authenticated, "u@example.com" if authenticated else None)),
            patch("app.controllers.auth._get_listened_count", return_value=0),
        ):
            with app.test_client() as c:
                with c.session_transaction() as sess:
                    sess["user_id"] = 1
                resp = c.get(url)
        db_session.expire_all()
        return resp.status_code, db_session.get(UserSettings, 1)

    return _get


# --- Q2 / F6: malformed name lists -----------------------------------------


@pytest.mark.parametrize("field, toggle", [
    ("excluded_authors_json", "exclude_authors"),
    ("excluded_narrators_json", "exclude_narrators"),
])
def test_non_string_list_items_are_dropped_not_saved(client_get, field, toggle):
    payload = json.dumps([{"a": 1}, 5, None, ["x"], "  Real   Name ", "", "   "])
    status, row = client_get(f"/?_submit=1&genre_id=5077&{toggle}=on&{field}={payload}")
    assert status == 200
    assert json.loads(getattr(row, field)) == ["Real Name"]
    # The next plain visit still works
    assert client_get("/")[0] == 200


def test_name_list_length_caps(client_get):
    names = [f"Name {i}" for i in range(500)] + ["x" * 1000]
    status, row = client_get(
        "/?_submit=1&genre_id=5077&exclude_authors=on&excluded_authors_json=" + json.dumps(names[:150])
    )
    assert status == 200
    saved = json.loads(row.excluded_authors_json)
    assert len(saved) == 100
    status, row = client_get(
        "/?_submit=1&genre_id=5077&exclude_authors=on&excluded_authors_json=" + json.dumps(["x" * 1000])
    )
    assert status == 200
    assert len(json.loads(row.excluded_authors_json)[0]) == 200


def test_already_saved_bad_settings_heal_on_load(client_get, db_session):
    row = db_session.get(UserSettings, 1)
    row.exclude_authors = True
    row.excluded_authors_json = '[{"a": 1}, "Keep Me", 7]'
    row.exclude_narrators = True
    row.excluded_narrators_json = "{not json"
    row.rating_max = float("inf")
    row.rating_min = -3.0
    row.series_min = 10**15
    db_session.flush()

    status, _ = client_get("/")
    assert status == 200
    q = SettingsService(db_session, user_id=1).get()
    assert q.excluded_authors == ["Keep Me"]
    assert q.excluded_narrators == []
    assert q.rating_max is None
    assert q.rating_min == 0.0
    assert q.series_min == 10_000


# --- Q3 / F7: unknown genre ------------------------------------------------


@pytest.mark.parametrize("gid", ["does-not-exist", "99999999", "x" * 100])
def test_unknown_genre_on_submit_falls_back_to_saved(client_get, gid):
    status, row = client_get(f"/?_submit=1&genre_id={gid}&only_series=on")
    assert status == 200
    assert row.genre_id == "5077"
    assert row.series_only is True


def test_unknown_genre_on_navigation_falls_back(client_get):
    with patch("app.controllers.catalog.get_catalog", wraps=__import__(
            "app.services.catalog_service", fromlist=["get_catalog"]).get_catalog) as gc:
        status, _ = client_get("/?genre_id=nope")
    assert status == 200
    assert gc.call_args.args[1].genre_id == "5077"


# --- Q4: integer overflow --------------------------------------------------


@pytest.mark.parametrize("url", [
    "/?page=99999999999999999999",
    "/?_submit=1&genre_id=5077&rating_count_min=99999999999999999999999",
    "/?_submit=1&genre_id=5077&only_series=on&series_min=99999999999999999999999",
    "/?_submit=1&genre_id=5077&only_series=on&series_max=-99999999999999999999999",
])
def test_huge_ints_do_not_500(client_get, url):
    status, row = client_get(url)
    assert status == 200
    for col in ("rating_count_min", "series_min", "series_max"):
        v = getattr(row, col)
        assert v is None or 0 <= v <= 10_000_000


# --- Q5: nan / inf ratings -------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("nan", None), ("inf", None), ("-inf", None), ("Infinity", None),
    ("7", 5.0), ("-1", 0.0), ("4.5", 4.5), ("abc", None),
])
def test_rating_bounds_are_finite_and_clamped(client_get, raw, expected):
    status, row = client_get(f"/?_submit=1&genre_id=5077&rating_min={raw}&rating_max={raw}")
    assert status == 200
    assert row.rating_min == expected
    assert row.rating_max == expected


# --- Q-S5: logged-out Apply keeps saved listened filters -------------------


def test_logged_out_apply_keeps_saved_listened_filters(client_get, db_session):
    row = db_session.get(UserSettings, 1)
    row.incomplete_series_only = True
    db_session.flush()
    status, row = client_get("/?_submit=1&genre_id=5077&only_series=on", authenticated=False)
    assert status == 200
    assert row.series_only is True
    assert row.incomplete_series_only is True
    # Logged in, the checkboxes are real input
    status, row = client_get("/?_submit=1&genre_id=5077&hide_listened=on", authenticated=True)
    assert row.incomplete_series_only is False
    assert row.hide_listened is True


# --- Q-S6: sort is persisted -----------------------------------------------


def test_sort_is_persisted_and_reused(client_get, db_session):
    status, row = client_get("/?_submit=1&genre_id=5077&sort=rating_avg_desc")
    assert status == 200 and row.sort == "rating_avg_desc"
    assert SettingsService(db_session, user_id=1).get().sort == "rating_avg_desc"
    # Default sort is omitted by the form
    _, row = client_get("/?_submit=1&genre_id=5077")
    assert row.sort == "release_date_desc"
    # Sort outside the form (no _submit) is remembered as well; junk is ignored
    _, row = client_get("/?sort=rating_count_desc")
    assert row.sort == "rating_count_desc"
    _, row = client_get("/?sort=drop table")
    assert row.sort == "release_date_desc"


def test_reset_keeps_sort(db_session):
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(user_id=1, sort="rating_count_desc", series_only=True))
    db_session.flush()
    q = SettingsService(db_session, user_id=1).reset()
    assert q.sort == "rating_count_desc"
    assert q.series_only is False


# --- Q9: F-6 default narrator list for new users --------------------------


def test_new_user_row_gets_default_narrator_list(db_session):
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(user_id=1, genre_id=None))  # what middleware creates
    db_session.flush()
    q = SettingsService(db_session, user_id=1).get()
    assert q.exclude_narrators is False
    assert q.excluded_narrators == ["Литрес Авточтец"]


def test_no_row_defaults_match_reset(db_session):
    q = SettingsService(db_session, user_id=999).get()
    assert q.exclude_narrators is False
    assert q.excluded_narrators == ["Литрес Авточтец"]


def test_saved_empty_narrator_list_stays_empty(db_session):
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(user_id=1, excluded_narrators_json="[]"))
    db_session.flush()
    assert SettingsService(db_session, user_id=1).get().excluded_narrators == []


def test_save_dedupes_case_insensitively_keeping_display_spelling(db_session):
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(user_id=1))
    db_session.flush()
    SettingsService(db_session, user_id=1).save(CatalogQuery(
        exclude_narrators=True,
        excluded_narrators=["Литрес Авточтец", "литрес  авточтец", "Пётр Ёлкин", "петр елкин"],
    ))
    row = db_session.get(UserSettings, 1)
    assert json.loads(row.excluded_narrators_json) == ["Литрес Авточтец", "Пётр Ёлкин"]
