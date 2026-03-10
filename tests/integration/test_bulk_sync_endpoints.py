"""Integration tests for Flask admin sync endpoints.

These tests use the Flask test client with the real test DB (no LitRes network calls).
Run manually:
    pytest tests/integration/test_bulk_sync_endpoints.py -v -s -m integration

Or together with bulk sync tests:
    pytest tests/integration/ -v -s -m integration
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import SyncConfig, SyncRun


@pytest.fixture
def endpoint_db():
    """Fresh in-memory DB with schema + SyncConfig, shared across endpoint tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with Factory() as s:
        s.add(SyncConfig(
            genre_slug="legkoe-chtenie",
            genre_id=201583,
            art_type="audiobook",
            language_code="ru",
            only_subscription=True,
        ))
        s.commit()

    return Factory


@pytest.fixture
def flask_client(endpoint_db):
    """Flask test client with admin blueprint, wired to endpoint_db."""
    from app.start import create_app
    application = create_app()
    application.config["TESTING"] = True

    with patch("app.middleware.SessionLocal", endpoint_db), \
         patch("app.controllers.admin.SessionLocal", endpoint_db), \
         patch("app.sync.bulk.SessionLocal", endpoint_db):
        with application.test_client() as c:
            yield c


@pytest.mark.integration
def test_status_never_run(endpoint_db):
    """GET /admin/sync/status with empty DB returns never_run."""
    from app.start import create_app
    application = create_app()
    application.config["TESTING"] = True

    # Empty DB — no SyncRun rows
    empty_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(empty_engine)
    EmptyFactory = sessionmaker(bind=empty_engine)

    with patch("app.middleware.SessionLocal", EmptyFactory), \
         patch("app.controllers.admin.SessionLocal", EmptyFactory):
        with application.test_client() as c:
            resp = c.get("/admin/sync/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "never_run"


@pytest.mark.integration
def test_start_bulk_returns_202(flask_client):
    """POST /admin/sync/bulk with dry_run=True returns 202."""
    resp = flask_client.post("/admin/sync/bulk", json={"max_pages": 1, "dry_run": True})
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "started"
    assert data["dry_run"] is True


@pytest.mark.integration
def test_concurrent_bulk_returns_409(endpoint_db):
    """Second POST while a sync is running returns 409."""
    from app.start import create_app
    application = create_app()
    application.config["TESTING"] = True

    # Insert a running SyncRun into the endpoint_db
    with endpoint_db() as s:
        config = s.query(SyncConfig).first()
        s.add(SyncRun(
            sync_config_id=config.id,
            type="bulk",
            started_at=datetime.now(timezone.utc),
            status="running",
            pages_fetched=0,
            books_upserted=0,
            series_fetched=0,
        ))
        s.commit()

    with patch("app.middleware.SessionLocal", endpoint_db), \
         patch("app.controllers.admin.SessionLocal", endpoint_db), \
         patch("app.sync.bulk.SessionLocal", endpoint_db):
        with application.test_client() as c:
            resp = c.post("/admin/sync/bulk", json={"max_pages": 1})
            assert resp.status_code == 409
            data = resp.get_json()
            assert "already running" in data["error"]


@pytest.mark.integration
def test_status_returns_latest_run(endpoint_db):
    """GET /admin/sync/status reflects the most recent SyncRun."""
    from app.start import create_app
    application = create_app()
    application.config["TESTING"] = True

    with endpoint_db() as s:
        config = s.query(SyncConfig).first()
        s.add(SyncRun(
            sync_config_id=config.id,
            type="bulk",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            status="done",
            pages_fetched=5,
            books_upserted=120,
            series_fetched=3,
        ))
        s.commit()

    with patch("app.middleware.SessionLocal", endpoint_db), \
         patch("app.controllers.admin.SessionLocal", endpoint_db):
        with application.test_client() as c:
            resp = c.get("/admin/sync/status")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "done"
            assert data["pages_fetched"] == 5
            assert data["books_upserted"] == 120
            assert data["series_fetched"] == 3
