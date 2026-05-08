"""Tests for UTCDateTime TypeDecorator and the middleware regression it fixes.

Production bug (2026-05-08): authenticated requests returned 500 because
SQLAlchemy's plain DateTime column on SQLite strips tzinfo on read, so
``datetime.now(timezone.utc) - row.started_at`` raised TypeError when
comparing aware vs naive datetimes.

The fix is the UTCDateTime TypeDecorator in app/db/base.py, which re-attaches
timezone.utc on read and normalizes to UTC on write. These tests pin both
behaviors so the bug cannot regress silently.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import SyncConfig, SyncRun, User, UserSettings


def _make_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class TestUTCDateTimeRoundTrip:
    def test_aware_utc_write_read_preserves_tz(self):
        factory = _make_factory()
        with factory() as s:
            s.add(SyncConfig(
                id=1, genre_slug="t", genre_id=1, art_type="audiobook",
                language_code="ru", only_subscription=True,
            ))
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            s.add(SyncRun(
                sync_config_id=1, type="profile", status="done",
                started_at=past,
                finished_at=past + timedelta(minutes=5),
            ))
            s.commit()

        with factory() as s:
            run = s.query(SyncRun).first()
            assert run.started_at.tzinfo is timezone.utc
            assert run.finished_at.tzinfo is timezone.utc
            # Arithmetic with datetime.now(timezone.utc) must work.
            delta = datetime.now(timezone.utc) - run.started_at
            assert delta.total_seconds() > 0

    def test_aware_non_utc_write_normalized_to_utc(self):
        factory = _make_factory()
        msk = timezone(timedelta(hours=3))
        with factory() as s:
            s.add(SyncConfig(
                id=1, genre_slug="t", genre_id=1, art_type="audiobook",
                language_code="ru", only_subscription=True,
            ))
            # 12:00 MSK == 09:00 UTC
            s.add(SyncRun(
                sync_config_id=1, type="profile", status="done",
                started_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=msk),
            ))
            s.commit()

        with factory() as s:
            run = s.query(SyncRun).first()
            assert run.started_at == datetime(2026, 5, 8, 9, 0, 0, tzinfo=timezone.utc)

    def test_legacy_naive_row_loaded_as_aware_utc(self):
        """Production rows written before this fix are stored without tzinfo.
        UTCDateTime must interpret them as UTC so existing data keeps working
        with no migration."""
        factory = _make_factory()
        with factory() as s:
            s.add(SyncConfig(
                id=1, genre_slug="t", genre_id=1, art_type="audiobook",
                language_code="ru", only_subscription=True,
            ))
            s.commit()
            # Bypass ORM to insert a naive ISO string, simulating legacy data.
            s.execute(text(
                "INSERT INTO sync_run "
                "(id, sync_config_id, type, started_at, status, "
                "pages_fetched, books_upserted, series_fetched, "
                "books_new, books_updated, books_failed) "
                "VALUES (1, 1, 'profile', '2026-05-02 11:48:03.123456', "
                "'done', 0, 0, 0, 0, 0, 0)"
            ))
            s.commit()

        with factory() as s:
            run = s.query(SyncRun).first()
            assert run.started_at.tzinfo is timezone.utc
            # The exact arithmetic that crashed in production must succeed.
            age = (datetime.now(timezone.utc) - run.started_at).total_seconds()
            assert age > 0


class TestMiddlewareTimezoneRegression:
    """Regression for bug-report.md (2026-05-08).

    Authenticated requests crashed in _maybe_background_profile_sync at
    ``datetime.now(timezone.utc) - last_profile_run.started_at`` because the
    DB-loaded started_at was naive. With UTCDateTime in place, the comparison
    just works.
    """

    def test_authenticated_request_with_legacy_sync_row_does_not_500(self):
        factory = _make_factory()
        with factory() as s:
            s.add(SyncConfig(
                id=1, genre_slug="t", genre_id=1, art_type="audiobook",
                language_code="ru", only_subscription=True,
            ))
            s.add(User(
                id=1, litres_login="user@example.com",
                session_data=json.dumps({
                    "access_token": "fake", "refresh_token": "fake",
                    "email": "user@example.com",
                }),
                created_at=datetime.now(timezone.utc),
            ))
            s.add(UserSettings(user_id=1))
            s.commit()
            # Legacy naive row, recent enough that freshness check returns
            # early (no background thread spawn).
            recent = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
            s.execute(text(
                "INSERT INTO sync_run "
                "(id, sync_config_id, type, started_at, status, "
                "pages_fetched, books_upserted, series_fetched, "
                "books_new, books_updated, books_failed) "
                "VALUES (1, 1, 'profile', :ts, 'done', 0, 0, 0, 0, 0, 0)"
            ), {"ts": recent.isoformat(sep=" ")})
            s.commit()

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        with patch("app.middleware.SessionLocal", factory):
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1
                resp = client.get("/")

        assert resp.status_code != 500, (
            f"Authenticated request crashed: {resp.status_code} {resp.data!r}"
        )
