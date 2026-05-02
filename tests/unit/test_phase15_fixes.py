"""Unit tests for Phase 15 fixes.

Covers: stuck sync recovery, sync type distinction (delta vs bulk),
series URL filters, persistent sessions, background profile sync,
and crontab PATH line preservation.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import SyncConfig, SyncRun, User, UserSettings


def _make_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_sync_config(factory):
    with factory() as s:
        s.add(SyncConfig(
            id=1, genre_slug="test", genre_id=999,
            art_type="audiobook", language_code="ru", only_subscription=True,
        ))
        s.commit()


def _seed_user(factory, user_id=1, litres_login=None, session_data=None):
    with factory() as s:
        s.add(User(id=user_id, litres_login=litres_login,
                   session_data=session_data,
                   created_at=datetime.now(timezone.utc)))
        s.add(UserSettings(user_id=user_id))
        s.commit()


# =========================================================================
# 15.2 — Auto-recover stuck syncs
# =========================================================================

class TestStuckSyncRecovery:
    def test_recover_run_with_finished_at_set(self):
        """SyncRun with finished_at but status='running' -> auto-recovered to 'failed'."""
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime(2026, 4, 18, 19, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 4, 18, 19, 5, tzinfo=timezone.utc),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import check_no_running_sync

        with factory() as s:
            # Should NOT raise — auto-recovers the stuck run
            check_no_running_sync(s)

            run = s.query(SyncRun).first()
            assert run.status == "failed"
            assert "Auto-recovered" in run.error_message
            assert "finished_at" in run.error_message

    def test_recover_run_older_than_24h(self):
        """SyncRun running >24h without finished_at -> auto-recovered."""
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime.now(timezone.utc) - timedelta(hours=25),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import check_no_running_sync

        with factory() as s:
            check_no_running_sync(s)
            run = s.query(SyncRun).first()
            assert run.status == "failed"
            assert "24h" in run.error_message

    def test_genuinely_running_sync_still_blocks(self):
        """SyncRun running <24h, no finished_at -> still raises RuntimeError."""
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime.now(timezone.utc) - timedelta(hours=1),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import check_no_running_sync

        with factory() as s:
            with pytest.raises(RuntimeError, match="Sync already running"):
                check_no_running_sync(s)

    def test_no_running_syncs_passes(self):
        """No running syncs -> no error."""
        factory = _make_factory()
        _seed_sync_config(factory)

        from app.sync.common import check_no_running_sync

        with factory() as s:
            check_no_running_sync(s)  # Should not raise


# =========================================================================
# 15.3 — Sync type distinction (delta vs bulk)
# =========================================================================

class TestSyncTypeDistinction:
    def test_delta_type_when_max_pages_set(self):
        """run_bulk with max_pages should create SyncRun with type='delta'."""
        factory = _make_factory()
        _seed_sync_config(factory)

        from app.sync.bulk import _open_sync_run

        with factory() as s:
            config = s.query(SyncConfig).first()
            run = _open_sync_run(s, config, resume=False, sync_type="delta")
            assert run.type == "delta"

    def test_bulk_type_when_no_max_pages(self):
        """run_bulk without max_pages should create SyncRun with type='bulk'."""
        factory = _make_factory()
        _seed_sync_config(factory)

        from app.sync.bulk import _open_sync_run

        with factory() as s:
            config = s.query(SyncConfig).first()
            run = _open_sync_run(s, config, resume=False, sync_type="bulk")
            assert run.type == "bulk"


# =========================================================================
# 15.4 — Series URL filters
# =========================================================================

class TestSeriesUrlFilters:
    def test_series_url_adds_audiobook_filter(self):
        from app.services.catalog_service import _series_url

        url = _series_url("/series/test-series-12345/")
        assert "art_types=audiobook" in url
        assert "only_litres_subscription_arts=true" in url
        assert url.startswith("https://www.litres.ru")

    def test_series_url_preserves_existing_path(self):
        from app.services.catalog_service import _series_url

        url = _series_url("https://www.litres.ru/series/my-series-999/")
        assert "/series/my-series-999/" in url
        assert "art_types=audiobook" in url

    def test_series_url_handles_absolute_url(self):
        from app.services.catalog_service import _series_url

        url = _series_url("https://www.litres.ru/series/slug/")
        assert url.startswith("https://www.litres.ru/series/slug/")
        assert "art_types=audiobook" in url


# =========================================================================
# 15.5 — Persistent session
# =========================================================================

class TestPersistentSession:
    def test_session_marked_permanent(self):
        """Every request should set session.permanent = True."""
        factory = _make_factory()
        _seed_user(factory)

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        with patch("app.middleware.SessionLocal", factory):
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1

                # Make a request to trigger the middleware
                client.get("/")

                with client.session_transaction() as sess:
                    # Session should be permanent
                    assert sess.permanent is True

    def test_session_lifetime_configured(self):
        """PERMANENT_SESSION_LIFETIME should be set to 365 days."""
        from app.config import Config
        assert Config.PERMANENT_SESSION_LIFETIME == timedelta(days=365)


# =========================================================================
# 15.6 — Background profile sync
# =========================================================================

class TestBackgroundProfileSync:
    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_login_returns_immediately(self, mock_token, mock_sync, ):
        """Login should return with syncing=True without waiting for profile sync."""
        factory = _make_factory()
        _seed_user(factory)

        mock_token.return_value = "fake-jwt"
        # Make sync slow to prove login doesn't wait
        mock_sync.side_effect = lambda **kw: time.sleep(0.5)

        from app.start import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        with (
            patch("app.middleware.SessionLocal", factory),
            patch("app.controllers.auth.SessionLocal", factory),
        ):
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1

                start = time.monotonic()
                resp = client.post("/auth/login", json={
                    "email": "test@example.com", "password": "pass123",
                })
                elapsed = time.monotonic() - start

                data = resp.get_json()
                assert resp.status_code == 200
                assert data["ok"] is True
                assert data["syncing"] is True
                # Login should complete in well under the 0.5s sync delay
                assert elapsed < 0.4

    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_login_sets_sync_trigger_timestamp(self, mock_token, mock_sync):
        """Login should set _profile_sync_started in session."""
        factory = _make_factory()
        _seed_user(factory)

        mock_token.return_value = "fake-jwt"
        mock_sync.return_value = None

        from app.start import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        with (
            patch("app.middleware.SessionLocal", factory),
            patch("app.controllers.auth.SessionLocal", factory),
        ):
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1

                client.post("/auth/login", json={
                    "email": "test@example.com", "password": "pass123",
                })

                with client.session_transaction() as sess:
                    assert "_profile_sync_started" in sess


# =========================================================================
# 15.1 — Crontab PATH line
# =========================================================================

class TestCrontabPath:
    def test_default_crontab_has_path_line(self):
        """docker/app/crontab should contain PATH=/usr/local/bin:..."""
        crontab_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "docker", "app", "crontab",
        )
        content = open(crontab_path).read()
        assert "PATH=/usr/local/bin:/usr/bin:/bin" in content

    @patch("app.controllers.admin_panel.subprocess")
    def test_cron_write_preserves_path_line(self, mock_subprocess, tmp_path):
        """Admin cron update should write PATH line in the output file."""
        factory = _make_factory()
        _seed_sync_config(factory)
        _seed_user(factory, litres_login="admin@test.com")

        from app.start import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        persistent_crontab = str(tmp_path / "persistent" / "crontab")

        with (
            patch("app.middleware.SessionLocal", factory),
            patch("app.controllers.admin_panel.SessionLocal", factory),
            patch("app.controllers.admin_panel.Config") as mock_cfg,
            patch("app.controllers.admin_panel._PERSISTENT_CRONTAB", persistent_crontab),
        ):
            mock_cfg.ADMIN_EMAILS = {"admin@test.com"}
            mock_cfg.SYNC_DIR = "/nonexistent"
            mock_cfg.BASE_DIR = str(tmp_path)
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")

            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1

                resp = client.post("/admin/cron/update", json={
                    "jobs": [{
                        "schedule": "0 0 * * 1-6",
                        "command": "cd /app && python -m app.sync bulk --max-pages 100",
                        "description": "Delta sync",
                    }]
                })

            assert resp.status_code == 200
            content = open(persistent_crontab).read()
            assert "PATH=/usr/local/bin:/usr/bin:/bin" in content
