"""Unit tests for SettingsService — load/save CatalogQuery from UserSettings."""

import json
from datetime import datetime, timezone

from app.db.models import User, UserSettings
from app.models.catalog_query import CatalogQuery
from app.services.settings_service import SettingsService


def test_get_returns_defaults_when_no_row(db_session):
    """get() returns default CatalogQuery when user has no settings row."""
    svc = SettingsService(db_session, user_id=999)
    q = svc.get()
    assert q.genre_id is None
    assert q.series_only is False
    assert q.sort == "release_date_desc"


def test_get_loads_all_fields(db_session):
    """get() correctly maps all UserSettings columns to CatalogQuery fields."""
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(
        user_id=1,
        genre_id="5077",
        series_only=True,
        standalones_only=False,
        series_min=3,
        series_max=10,
        full_series_subscription=True,
        exclude_authors=True,
        excluded_authors_json=json.dumps(["Author A"]),
        exclude_narrators=True,
        excluded_narrators_json=json.dumps(["Литрес Авточтец"]),
        rating_min=3.5,
        rating_max=5.0,
    ))
    db_session.flush()

    svc = SettingsService(db_session, user_id=1)
    q = svc.get()
    assert q.genre_id == "5077"
    assert q.series_only is True
    assert q.series_min == 3
    assert q.series_max == 10
    assert q.full_series_subscription is True
    assert q.exclude_authors is True
    assert q.excluded_authors == ["Author A"]
    assert q.exclude_narrators is True
    assert q.excluded_narrators == ["Литрес Авточтец"]
    assert q.rating_min == 3.5
    assert q.rating_max == 5.0


def test_save_persists_all_fields(db_session):
    """save() writes all CatalogQuery fields to UserSettings."""
    db_session.add(User(id=1, created_at=datetime.now(timezone.utc)))
    db_session.add(UserSettings(user_id=1))
    db_session.flush()

    svc = SettingsService(db_session, user_id=1)
    q = CatalogQuery(
        genre_id="5004",
        series_only=True,
        exclude_narrators=True,
        excluded_narrators=["Авточтец"],
        rating_min=4.0,
    )
    svc.save(q)

    row = db_session.get(UserSettings, 1)
    assert row.genre_id == "5004"
    assert row.series_only is True
    assert row.exclude_narrators is True
    assert json.loads(row.excluded_narrators_json) == ["Авточтец"]
    assert row.rating_min == 4.0
    assert row.updated_at is not None
