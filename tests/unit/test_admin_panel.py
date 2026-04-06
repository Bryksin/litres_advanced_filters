"""Unit tests for the admin panel controller.

Covers admin auth (before_request guard), dashboard rendering,
crontab read/write, failed books parsing, sync retry validation,
and sync status with new SyncRun fields.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import SyncConfig, SyncRun, User, UserSettings

# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------

_ADMIN_EMAIL = "admin@litres.test"
_NON_ADMIN_EMAIL = "reader@litres.test"


def _make_engine_and_factory():
    """Create a fresh in-memory SQLite DB with the full schema."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    return engine, Factory


def _seed_sync_config(factory):
    """Insert a minimal SyncConfig row (required FK for SyncRun)."""
    with factory() as s:
        s.add(
            SyncConfig(
                id=1,
                genre_slug="test-genre",
                genre_id=999,
                art_type="audiobook",
                language_code="ru",
                only_subscription=True,
            )
        )
        s.commit()


def _seed_user(factory, *, user_id=1, litres_login=None, session_data=None):
    """Insert a user + settings into the test DB."""
    with factory() as s:
        s.add(
            User(
                id=user_id,
                litres_login=litres_login,
                session_data=session_data,
                created_at=datetime.now(timezone.utc),
            )
        )
        s.add(UserSettings(user_id=user_id))
        s.commit()


def _build_app(factory, *, admin_emails=None, sync_dir=None):
    """Build a Flask app with admin_panel + middleware wired to the given factory.

    Returns the app object.  The caller is responsible for entering
    the appropriate patch context managers if needed (this helper keeps
    patches active for the lifetime of the app via the returned object).
    """
    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    return app


@pytest.fixture
def admin_env():
    """Yield (app, Factory) with one admin user and admin blueprint enabled.

    Patches Config.ADMIN_EMAILS, SessionLocal in middleware and admin_panel,
    and Config.SYNC_DIR to a dummy path (overridden per-test when needed).
    """
    engine, Factory = _make_engine_and_factory()
    _seed_sync_config(Factory)
    _seed_user(Factory, litres_login=_ADMIN_EMAIL)

    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"

    with (
        patch("app.middleware.SessionLocal", Factory),
        patch("app.controllers.admin_panel.SessionLocal", Factory),
        patch("app.controllers.auth.SessionLocal", Factory),
        patch("app.controllers.admin_panel.Config") as mock_cfg,
    ):
        mock_cfg.ADMIN_EMAILS = {_ADMIN_EMAIL}
        mock_cfg.SYNC_DIR = "/nonexistent"
        mock_cfg.BASE_DIR = "/nonexistent"
        yield app, Factory, mock_cfg


def _admin_client(app, *, user_id=1):
    """Return a test client with session pre-set to the given user_id."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return client


# =========================================================================
# Admin auth tests
# =========================================================================


class TestAdminAuth:
    """Tests for the admin before_request guard."""

    def test_admin_disabled_when_no_emails_configured(self):
        """ADMIN_EMAILS=None -> GET /admin/ returns 404."""
        engine, Factory = _make_engine_and_factory()
        _seed_sync_config(Factory)
        _seed_user(Factory, litres_login=_ADMIN_EMAIL)

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True

        with (
            patch("app.middleware.SessionLocal", Factory),
            patch("app.controllers.admin_panel.SessionLocal", Factory),
            patch("app.controllers.admin_panel.Config") as mock_cfg,
        ):
            mock_cfg.ADMIN_EMAILS = None
            client = _admin_client(app)
            resp = client.get("/admin/")
            assert resp.status_code == 404

    def test_admin_403_when_not_logged_in(self, admin_env):
        """ADMIN_EMAILS set, user has no litres_login -> 403."""
        app, Factory, _ = admin_env

        # Create a second user with no litres_login
        _seed_user(Factory, user_id=2, litres_login=None)
        client = _admin_client(app, user_id=2)
        resp = client.get("/admin/")
        assert resp.status_code == 403

    def test_admin_403_when_non_admin_email(self, admin_env):
        """User logged in with email not in ADMIN_EMAILS -> 403."""
        app, Factory, _ = admin_env

        _seed_user(Factory, user_id=3, litres_login=_NON_ADMIN_EMAIL)
        client = _admin_client(app, user_id=3)
        resp = client.get("/admin/")
        assert resp.status_code == 403

    def test_admin_accessible_with_admin_email(self, admin_env):
        """User logged in with admin email -> 200."""
        app, Factory, mock_cfg = admin_env
        # read_crontab and read_failed_books both need reasonable defaults
        mock_cfg.SYNC_DIR = "/nonexistent"
        mock_cfg.BASE_DIR = "/nonexistent"

        client = _admin_client(app)
        resp = client.get("/admin/")
        assert resp.status_code == 200

    def test_admin_email_case_insensitive(self):
        """User email 'Alice@Example.COM' matches ADMIN_EMAILS={'alice@example.com'}."""
        engine, Factory = _make_engine_and_factory()
        _seed_sync_config(Factory)
        _seed_user(Factory, litres_login="Alice@Example.COM")

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True

        with (
            patch("app.middleware.SessionLocal", Factory),
            patch("app.controllers.admin_panel.SessionLocal", Factory),
            patch("app.controllers.admin_panel.Config") as mock_cfg,
        ):
            mock_cfg.ADMIN_EMAILS = {"alice@example.com"}
            mock_cfg.SYNC_DIR = "/nonexistent"
            mock_cfg.BASE_DIR = "/nonexistent"
            client = _admin_client(app)
            resp = client.get("/admin/")
            assert resp.status_code == 200

    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_admin_flag_set_on_login(self, mock_token, mock_sync):
        """After successful LitRes login with admin email, session['is_admin'] is True."""
        engine, Factory = _make_engine_and_factory()
        _seed_sync_config(Factory)
        _seed_user(Factory, litres_login=None)
        mock_token.return_value = "fake-jwt"
        mock_sync.return_value = None

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        with (
            patch("app.middleware.SessionLocal", Factory),
            patch("app.controllers.auth.SessionLocal", Factory),
            patch("app.controllers.auth.Config") as mock_auth_cfg,
        ):
            mock_auth_cfg.ADMIN_EMAILS = {_ADMIN_EMAIL}
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1

                client.post(
                    "/auth/login",
                    json={"email": _ADMIN_EMAIL, "password": "pass123"},
                )

                with client.session_transaction() as sess:
                    assert sess.get("is_admin") is True

    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_admin_flag_cleared_on_logout(self, mock_token, mock_sync):
        """After logout, session['is_admin'] is gone."""
        engine, Factory = _make_engine_and_factory()
        _seed_sync_config(Factory)
        _seed_user(Factory, litres_login=_ADMIN_EMAIL)
        mock_token.return_value = "fake-jwt"
        mock_sync.return_value = None

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"

        with (
            patch("app.middleware.SessionLocal", Factory),
            patch("app.controllers.auth.SessionLocal", Factory),
            patch("app.controllers.auth.Config") as mock_auth_cfg,
        ):
            mock_auth_cfg.ADMIN_EMAILS = {_ADMIN_EMAIL}
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["user_id"] = 1
                    sess["is_admin"] = True

                client.post("/auth/logout")

                with client.session_transaction() as sess:
                    assert "is_admin" not in sess


# =========================================================================
# Dashboard data tests
# =========================================================================


class TestDashboardData:
    """Tests for data rendered on the admin dashboard."""

    def test_dashboard_shows_last_sync(self, admin_env):
        """Insert a SyncRun, verify dashboard renders its stats."""
        app, Factory, mock_cfg = admin_env
        mock_cfg.SYNC_DIR = "/nonexistent"
        mock_cfg.BASE_DIR = "/nonexistent"

        with Factory() as s:
            s.add(
                SyncRun(
                    sync_config_id=1,
                    type="bulk",
                    started_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 4, 1, 10, 30, 0, tzinfo=timezone.utc),
                    status="done",
                    pages_fetched=5,
                    books_upserted=120,
                    series_fetched=3,
                    books_new=80,
                    books_updated=30,
                    books_failed=10,
                )
            )
            s.commit()

        client = _admin_client(app)
        resp = client.get("/admin/")
        assert resp.status_code == 200

        html = resp.data.decode()
        # Verify key stats appear in the rendered page
        assert "done" in html
        assert "80" in html  # books_new
        assert "30" in html  # books_updated
        assert "10" in html  # books_failed

    def test_dashboard_empty_state(self, admin_env):
        """No SyncRun rows -> shows empty state message."""
        app, Factory, mock_cfg = admin_env
        mock_cfg.SYNC_DIR = "/nonexistent"
        mock_cfg.BASE_DIR = "/nonexistent"

        client = _admin_client(app)
        resp = client.get("/admin/")
        assert resp.status_code == 200

        html = resp.data.decode()
        # Russian empty-state text from template
        assert "ещё не выполнялась" in html

    def test_dashboard_shows_error_message(self, admin_env):
        """SyncRun with status='failed' and error_message -> rendered in page."""
        app, Factory, mock_cfg = admin_env
        mock_cfg.SYNC_DIR = "/nonexistent"
        mock_cfg.BASE_DIR = "/nonexistent"

        with Factory() as s:
            s.add(
                SyncRun(
                    sync_config_id=1,
                    type="bulk",
                    started_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 4, 1, 10, 5, 0, tzinfo=timezone.utc),
                    status="failed",
                    pages_fetched=2,
                    books_upserted=0,
                    series_fetched=0,
                    error_message="Connection timed out after 30s",
                )
            )
            s.commit()

        client = _admin_client(app)
        resp = client.get("/admin/")
        assert resp.status_code == 200

        html = resp.data.decode()
        assert "failed" in html
        assert "Connection timed out after 30s" in html


# =========================================================================
# Cron tests
# =========================================================================


class TestCron:
    """Tests for read_crontab() and the cron update endpoint."""

    def test_read_crontab_parses_correctly(self, tmp_path):
        """Write a sample crontab file, call read_crontab(), verify parsed output."""
        crontab_content = (
            "# Delta sync (nightly)\n"
            "0 3 * * 1-6  cd /app && python -m app.sync bulk --max-pages 100\n"
            "\n"
            "# Full sync (Sunday)\n"
            "0 2 * * 0  cd /app && python -m app.sync bulk --verbose\n"
        )
        crontab_file = tmp_path / "crontab"
        crontab_file.write_text(crontab_content)

        from app.controllers.admin_panel import read_crontab

        with patch(
            "app.controllers.admin_panel._crontab_path",
            return_value=str(crontab_file),
        ):
            jobs = read_crontab()

        assert len(jobs) == 2

        assert jobs[0]["description"] == "Delta sync (nightly)"
        assert jobs[0]["schedule"] == "0 3 * * 1-6"
        assert "max-pages 100" in jobs[0]["command"]

        assert jobs[1]["description"] == "Full sync (Sunday)"
        assert jobs[1]["schedule"] == "0 2 * * 0"

    @patch("app.controllers.admin_panel.subprocess")
    def test_cron_update_writes_file(self, mock_subprocess, admin_env, tmp_path):
        """POST /admin/cron/update with valid data -> file written correctly."""
        app, Factory, mock_cfg = admin_env

        persistent_crontab = str(tmp_path / "persistent" / "crontab")
        mock_cfg.BASE_DIR = str(tmp_path)

        # Patch the module-level _PERSISTENT_CRONTAB to our tmp location
        with patch(
            "app.controllers.admin_panel._PERSISTENT_CRONTAB", persistent_crontab
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")

            client = _admin_client(app)
            resp = client.post(
                "/admin/cron/update",
                json={
                    "jobs": [
                        {
                            "schedule": "0 3 * * 1-6",
                            "command": "cd /app && python -m app.sync bulk",
                            "description": "Delta sync",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["jobs"] == 1

        # Verify file was written
        assert os.path.isfile(persistent_crontab)
        content = open(persistent_crontab).read()
        assert "0 3 * * 1-6" in content
        assert "Delta sync" in content

    def test_cron_update_invalid_expression(self, admin_env):
        """POST with bad cron expression -> 400 error."""
        app, Factory, _ = admin_env

        client = _admin_client(app)
        resp = client.post(
            "/admin/cron/update",
            json={
                "jobs": [
                    {
                        "schedule": "not a cron",
                        "command": "echo hello",
                    }
                ]
            },
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "invalid cron expression" in data["error"]

    def test_cron_update_requires_admin(self):
        """POST without admin session -> 403 or 404."""
        engine, Factory = _make_engine_and_factory()
        _seed_sync_config(Factory)
        _seed_user(Factory, litres_login=_NON_ADMIN_EMAIL)

        from app.start import create_app

        app = create_app()
        app.config["TESTING"] = True

        with (
            patch("app.middleware.SessionLocal", Factory),
            patch("app.controllers.admin_panel.SessionLocal", Factory),
            patch("app.controllers.admin_panel.Config") as mock_cfg,
        ):
            mock_cfg.ADMIN_EMAILS = {_ADMIN_EMAIL}
            client = _admin_client(app)
            resp = client.post(
                "/admin/cron/update",
                json={"jobs": [{"schedule": "0 3 * * *", "command": "echo hi"}]},
            )
            assert resp.status_code == 403


# =========================================================================
# Failed books tests
# =========================================================================


class TestFailedBooks:
    """Tests for read_failed_books() and retry endpoint."""

    def test_read_failed_books_parses_jsonl(self, tmp_path):
        """Write a sample JSONL file, call read_failed_books(), verify parsed output."""
        sync_dir = tmp_path / "sync"
        sync_dir.mkdir()
        jsonl_file = sync_dir / "failed_books_20260401_100000.jsonl"
        records = [
            {"book_id": 123, "title": "Test Book", "error": "Timeout", "page_number": 5},
            {"book_id": 456, "title": "Another Book", "error": "404 Not Found", "page_number": 12},
        ]
        jsonl_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        from app.controllers.admin_panel import read_failed_books

        result, filename = read_failed_books(str(sync_dir))

        assert len(result) == 2
        assert result[0]["book_id"] == 123
        assert result[0]["title"] == "Test Book"
        assert result[0]["error"] == "Timeout"
        assert result[1]["book_id"] == 456
        assert filename == "failed_books_20260401_100000.jsonl"

    def test_read_failed_books_empty(self, tmp_path):
        """No JSONL files -> empty list."""
        sync_dir = tmp_path / "sync"
        sync_dir.mkdir()

        from app.controllers.admin_panel import read_failed_books

        result, filename = read_failed_books(str(sync_dir))

        assert result == []
        assert filename is None

    def test_retry_validates_filename(self, admin_env):
        """POST /admin/sync/retry with path traversal attempt -> 400."""
        app, Factory, _ = admin_env

        client = _admin_client(app)

        # Path traversal attempt
        resp = client.post(
            "/admin/sync/retry",
            json={"failed_file": "../../../etc/passwd"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Invalid filename" in data["error"]

    def test_retry_validates_empty_filename(self, admin_env):
        """POST /admin/sync/retry with empty filename -> 400."""
        app, Factory, _ = admin_env

        client = _admin_client(app)
        resp = client.post("/admin/sync/retry", json={"failed_file": ""})
        assert resp.status_code == 400

    def test_retry_validates_wrong_pattern(self, admin_env):
        """POST /admin/sync/retry with filename not matching pattern -> 400."""
        app, Factory, _ = admin_env

        client = _admin_client(app)
        resp = client.post(
            "/admin/sync/retry",
            json={"failed_file": "some_random_file.txt"},
        )
        assert resp.status_code == 400


# =========================================================================
# Sync stats tests
# =========================================================================


class TestSyncStatus:
    """Tests for /admin/sync/status with new SyncRun fields."""

    def test_sync_status_includes_new_fields(self, admin_env):
        """Insert SyncRun with books_new/updated/failed, GET /admin/sync/status includes them."""
        app, Factory, _ = admin_env

        with Factory() as s:
            s.add(
                SyncRun(
                    sync_config_id=1,
                    type="bulk",
                    started_at=datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 4, 1, 10, 30, 0, tzinfo=timezone.utc),
                    status="done",
                    pages_fetched=10,
                    books_upserted=200,
                    series_fetched=5,
                    books_new=150,
                    books_updated=40,
                    books_failed=10,
                )
            )
            s.commit()

        client = _admin_client(app)
        resp = client.get("/admin/sync/status")
        assert resp.status_code == 200

        data = resp.get_json()
        assert data["status"] == "done"
        assert data["pages_fetched"] == 10
        assert data["books_upserted"] == 200
        assert data["series_fetched"] == 5

    def test_sync_status_never_run(self, admin_env):
        """GET /admin/sync/status with no SyncRun rows returns never_run."""
        app, Factory, _ = admin_env

        client = _admin_client(app)
        resp = client.get("/admin/sync/status")
        assert resp.status_code == 200

        data = resp.get_json()
        assert data["status"] == "never_run"
