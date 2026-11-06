"""Phase 16: LitRes session expiry detection, auto re-login and auto-logout.

No network: httpx clients and token functions are mocked.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from flask import Flask, render_template_string
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Config
from app.db.base import Base
from app.db.models import Book, SyncConfig, User, UserListenedBook, UserSettings
from app.scrapers.auth import (
    AUTH_STATE_KEY,
    EXPIRED_NOTICE_KEY,
    PROFILE_SYNCED_AT_KEY,
    AuthResult,
    LitresAuthError,
    LitresAuthRejectedError,
    LitresAuthTransientError,
)

EMAIL = "reader@example.com"


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _session_data(
    *, token_ttl=timedelta(minutes=10), synced_ago=None, email=EMAIL
) -> str:
    data = {
        "email": email,
        "access_token": "jwt",
        "refresh_token": "r" * 40,
        "expires_at": _iso(token_ttl),
    }
    if synced_ago is not None:
        data[PROFILE_SYNCED_AT_KEY] = _iso(-synced_ago)
    return json.dumps(data)


def _resp(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    return resp


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Factory() as s:
        s.add(
            SyncConfig(
                genre_slug="legkoe-chtenie",
                genre_id=201583,
                art_type="audiobook",
                language_code="ru",
                only_subscription=True,
            )
        )
        s.add(User(id=1, created_at=datetime.now(timezone.utc), litres_login=EMAIL))
        s.add(UserSettings(user_id=1))
        for bid in (100, 200):
            s.add(
                Book(
                    id=bid,
                    title=f"B{bid}",
                    url=f"/b/{bid}",
                    art_type=1,
                    cached_at=datetime.now(timezone.utc),
                )
            )
        s.commit()
    return Factory


def _set_user(Factory, session_data, listened=()):
    with Factory() as s:
        user = s.get(User, 1)
        user.session_data = session_data
        for bid in listened:
            s.add(
                UserListenedBook(
                    user_id=1, book_id=bid, listened_at=datetime.now(timezone.utc)
                )
            )
        s.commit()


def _user_state(Factory):
    with Factory() as s:
        user = s.get(User, 1)
        data = json.loads(user.session_data) if user.session_data else None
        listened = s.query(UserListenedBook).filter_by(user_id=1).count()
        return data, user.litres_login, listened


@pytest.fixture
def no_env_creds():
    with (
        patch.object(Config, "LITRES_EMAIL", None),
        patch.object(Config, "LITRES_PASSWORD", None),
    ):
        yield


# ---------------------------------------------------------------------------
# Scraper error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_refresh_404_is_rejected(self):
        from app.scrapers.auth import litres_refresh

        client = MagicMock()
        client.post.return_value = _resp(404)
        with pytest.raises(LitresAuthRejectedError, match="refresh failed"):
            litres_refresh(client, "x")

    def test_refresh_5xx_is_transient(self):
        from app.scrapers.auth import litres_refresh

        client = MagicMock()
        client.post.return_value = _resp(503)
        with pytest.raises(LitresAuthTransientError):
            litres_refresh(client, "x")

    def test_refresh_network_error_is_transient(self):
        from app.scrapers.auth import litres_refresh

        client = MagicMock()
        client.post.side_effect = httpx.ConnectError("down")
        with pytest.raises(LitresAuthTransientError):
            litres_refresh(client, "x")

    def test_login_401_is_rejected_and_network_is_transient(self):
        from app.scrapers.auth import litres_login

        client = MagicMock()
        client.post.return_value = _resp(401)
        with pytest.raises(LitresAuthRejectedError, match="wrong email or password"):
            litres_login(client, EMAIL, "bad")
        client.post.side_effect = httpx.ConnectError("down")
        with pytest.raises(LitresAuthTransientError):
            litres_login(client, EMAIL, "pw")

    def test_login_captures_sid(self):
        from app.scrapers.auth import litres_login

        client = MagicMock()
        client.post.return_value = _resp(
            200,
            {
                "payload": {
                    "data": {
                        "access_token": "a",
                        "refresh_token": "r",
                        "expires_in": 900,
                        "sid": "s1",
                    }
                }
            },
        )
        assert litres_login(client, EMAIL, "pw").sid == "s1"


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    @patch("app.scrapers.auth.litres_login")
    @patch("app.scrapers.auth.litres_refresh")
    def test_expired_no_password_refresh_rejected_raises_rejected(
        self,
        mock_refresh,
        mock_login,
        db,
        no_env_creds,
    ):
        from app.scrapers.auth import get_valid_token

        _set_user(db, _session_data(token_ttl=timedelta(days=-3)))
        mock_refresh.side_effect = LitresAuthRejectedError(
            "Token refresh failed: HTTP 404"
        )
        with pytest.raises(LitresAuthRejectedError):
            get_valid_token(session_factory=db, user_id=1, email=EMAIL)
        mock_login.assert_not_called()

    @patch("app.scrapers.auth.litres_refresh")
    def test_expired_no_password_refresh_transient_raises_transient(
        self, mock_refresh, db, no_env_creds
    ):
        from app.scrapers.auth import get_valid_token

        _set_user(db, _session_data(token_ttl=timedelta(days=-3)))
        mock_refresh.side_effect = LitresAuthTransientError("down")
        with pytest.raises(LitresAuthTransientError):
            get_valid_token(session_factory=db, user_id=1, email=EMAIL)

    @patch("app.scrapers.auth.litres_login")
    @patch("app.scrapers.auth.litres_refresh")
    def test_env_credentials_used_for_matching_email(
        self, mock_refresh, mock_login, db
    ):
        from app.scrapers.auth import get_valid_token

        _set_user(
            db,
            _session_data(token_ttl=timedelta(days=-3), synced_ago=timedelta(days=3)),
        )
        mock_refresh.side_effect = LitresAuthRejectedError("404")
        mock_login.return_value = AuthResult("new-jwt", "new-r", 900)
        with (
            patch.object(Config, "LITRES_EMAIL", "Reader@Example.com"),
            patch.object(Config, "LITRES_PASSWORD", "env-pw"),
        ):
            token = get_valid_token(session_factory=db, user_id=1, email=EMAIL)

        assert token == "new-jwt"
        assert mock_login.call_args.args[1:] == (EMAIL, "env-pw")
        data, _, _ = _user_state(db)
        assert data["access_token"] == "new-jwt"
        assert PROFILE_SYNCED_AT_KEY in data  # preserved for the same account

    @patch("app.scrapers.auth.litres_refresh")
    def test_env_credentials_not_used_for_other_email(self, mock_refresh, db):
        from app.scrapers.auth import get_valid_token

        _set_user(db, _session_data(token_ttl=timedelta(days=-3)))
        mock_refresh.side_effect = LitresAuthRejectedError("404")
        with (
            patch.object(Config, "LITRES_EMAIL", "operator@example.com"),
            patch.object(Config, "LITRES_PASSWORD", "env-pw"),
            pytest.raises(LitresAuthRejectedError),
        ):
            get_valid_token(session_factory=db, user_id=1, email=EMAIL)

    @patch("app.scrapers.auth.litres_refresh")
    @patch("app.scrapers.auth.litres_login")
    def test_force_login_ignores_valid_stored_token(self, mock_login, mock_refresh, db):
        from app.scrapers.auth import get_valid_token

        _set_user(
            db, _session_data(email="other@example.com", synced_ago=timedelta(hours=1))
        )
        mock_login.return_value = AuthResult("fresh", "fresh-r", 900)
        token = get_valid_token(
            session_factory=db,
            user_id=1,
            email=EMAIL,
            password="pw",
            force_login=True,
        )
        assert token == "fresh"
        mock_refresh.assert_not_called()
        data, _, _ = _user_state(db)
        assert data["email"] == EMAIL
        assert PROFILE_SYNCED_AT_KEY not in data  # different account → stale

    @patch("app.scrapers.auth.litres_refresh")
    def test_force_renew_skips_unexpired_token(self, mock_refresh, db, no_env_creds):
        from app.scrapers.auth import get_valid_token

        _set_user(db, _session_data())
        mock_refresh.return_value = AuthResult("renewed", "r2", 900)
        assert (
            get_valid_token(
                session_factory=db, user_id=1, email=EMAIL, force_renew=True
            )
            == "renewed"
        )


class TestExpireSession:
    def test_expire_drops_tokens_but_keeps_account_and_listened(self, db):
        from app.scrapers.auth import expire_litres_session, is_litres_session_expired

        _set_user(db, _session_data(synced_ago=timedelta(days=3)), listened=(100, 200))
        assert expire_litres_session(db, 1) is True
        data, login, listened = _user_state(db)
        assert "access_token" not in data and "refresh_token" not in data
        assert data["email"] == EMAIL
        assert data[AUTH_STATE_KEY] == "expired"
        assert data[EXPIRED_NOTICE_KEY] is True
        assert PROFILE_SYNCED_AT_KEY in data
        assert is_litres_session_expired(data)
        assert login == EMAIL
        assert listened == 2

    def test_expire_is_idempotent(self, db):
        from app.scrapers.auth import expire_litres_session

        _set_user(db, _session_data())
        assert expire_litres_session(db, 1) is True
        assert expire_litres_session(db, 1) is False

    def test_expire_noop_when_not_logged_in(self, db):
        from app.scrapers.auth import expire_litres_session

        assert expire_litres_session(db, 1) is False
        assert _user_state(db)[0] is None


# ---------------------------------------------------------------------------
# run_profile
# ---------------------------------------------------------------------------


class TestRunProfileReauth:
    @patch("app.sync.profile.fetch_finished_book_ids")
    @patch("app.sync.profile.get_valid_token")
    def test_401_renews_token_once_and_succeeds(self, mock_token, mock_fetch, db):
        from app.scrapers.profile import LitresProfileAuthError
        from app.sync.profile import run_profile

        _set_user(db, _session_data())
        mock_token.side_effect = ["old", "new"]
        mock_fetch.side_effect = [LitresProfileAuthError("401"), [100]]
        run_profile(session_factory=db, email=EMAIL, user_id=1)

        assert mock_token.call_args_list[1].kwargs["force_renew"] is True
        data, _, listened = _user_state(db)
        assert listened == 1
        assert PROFILE_SYNCED_AT_KEY in data

    @patch("app.sync.profile.fetch_finished_book_ids")
    @patch("app.sync.profile.get_valid_token")
    def test_repeated_401_expires_session_when_asked(self, mock_token, mock_fetch, db):
        from app.scrapers.profile import LitresProfileAuthError
        from app.sync.profile import run_profile

        _set_user(db, _session_data(), listened=(100,))
        mock_token.return_value = "jwt"
        mock_fetch.side_effect = LitresProfileAuthError("401")
        with pytest.raises(LitresAuthRejectedError):
            run_profile(
                session_factory=db, email=EMAIL, user_id=1, expire_on_auth_failure=True
            )
        data, login, listened = _user_state(db)
        assert data[AUTH_STATE_KEY] == "expired" and "access_token" not in data
        assert login == EMAIL
        assert listened == 1

    @patch("app.sync.profile.get_valid_token")
    def test_rejection_without_flag_keeps_session(self, mock_token, db):
        from app.sync.profile import run_profile

        _set_user(db, _session_data())
        mock_token.side_effect = LitresAuthRejectedError("no password")
        with pytest.raises(LitresAuthRejectedError):
            run_profile(session_factory=db, email=EMAIL, user_id=1)
        assert _user_state(db)[0]["access_token"] == "jwt"

    @patch("app.sync.profile.get_valid_token")
    def test_transient_failure_never_expires_session(self, mock_token, db):
        from app.sync.profile import run_profile

        _set_user(db, _session_data())
        mock_token.side_effect = LitresAuthTransientError("down")
        with pytest.raises(LitresAuthError):
            run_profile(
                session_factory=db, email=EMAIL, user_id=1, expire_on_auth_failure=True
            )
        assert _user_state(db)[0]["access_token"] == "jwt"


# ---------------------------------------------------------------------------
# Middleware: page-open detection
# ---------------------------------------------------------------------------


@pytest.fixture
def mw_app(db):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    app.config["PROFILE_SYNC_STALE_HOURS"] = 20

    with patch("app.middleware.SessionLocal", db):
        from app.middleware import register_session_middleware

        register_session_middleware(app)

        @app.route("/page")
        def page():
            from app.controllers.auth import _is_authenticated

            is_auth, _ = _is_authenticated(db, 1)
            return render_template_string(
                "{{ ('EXPIRED' if auth_expired else 'IN') if a else 'OUT' }}"
                "|{{ auth_notice or '' }}",
                a=is_auth,
            )

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
        yield client


class TestMiddlewareExpiry:
    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_stale_and_unrenewable_marks_expired_with_notice(
        self, mock_token, mock_thread, mw_app, db, no_env_creds
    ):
        from app.middleware import AUTH_EXPIRED_MESSAGE

        _set_user(
            db,
            _session_data(token_ttl=timedelta(days=-3), synced_ago=timedelta(days=3)),
            listened=(100,),
        )
        mock_token.side_effect = LitresAuthRejectedError(
            "Token refresh failed: HTTP 404"
        )

        body = mw_app.get("/page").get_data(as_text=True)
        assert body == f"EXPIRED|{AUTH_EXPIRED_MESSAGE}"
        data, login, listened = _user_state(db)
        assert "access_token" not in data and EXPIRED_NOTICE_KEY not in data
        assert login == EMAIL and listened == 1  # cached data kept
        mock_thread.assert_not_called()

        # Notice is shown once; the expired badge stays; no further auth checks
        mock_token.reset_mock()
        assert mw_app.get("/page").get_data(as_text=True) == "EXPIRED|"
        mock_token.assert_not_called()

    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_transient_failure_keeps_login(
        self, mock_token, mock_thread, mw_app, db, no_env_creds
    ):
        _set_user(
            db,
            _session_data(token_ttl=timedelta(days=-3), synced_ago=timedelta(days=3)),
        )
        mock_token.side_effect = LitresAuthTransientError("down")
        assert mw_app.get("/page").get_data(as_text=True) == "IN|"
        mock_thread.assert_not_called()

    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_valid_token_stale_sync_starts_background_sync(
        self, mock_token, mock_thread, mw_app, db, no_env_creds
    ):
        _set_user(db, _session_data(synced_ago=timedelta(days=3)))
        assert mw_app.get("/page").get_data(as_text=True) == "IN|"
        mock_token.assert_not_called()
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()

    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_env_creds_defer_to_background_relogin(
        self, mock_token, mock_thread, mw_app, db
    ):
        _set_user(
            db,
            _session_data(token_ttl=timedelta(days=-3), synced_ago=timedelta(days=3)),
        )
        with (
            patch.object(Config, "LITRES_EMAIL", EMAIL),
            patch.object(Config, "LITRES_PASSWORD", "pw"),
        ):
            assert mw_app.get("/page").get_data(as_text=True) == "IN|"
        mock_token.assert_not_called()
        mock_thread.assert_called_once()

    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_fresh_sync_makes_no_auth_check(
        self, mock_token, mock_thread, mw_app, db, no_env_creds
    ):
        _set_user(
            db,
            _session_data(token_ttl=timedelta(days=-1), synced_ago=timedelta(hours=1)),
        )
        assert mw_app.get("/page").get_data(as_text=True) == "IN|"
        mock_token.assert_not_called()
        mock_thread.assert_not_called()

    def test_background_marker_becomes_notice_once(self, mw_app, db):
        from app.middleware import AUTH_EXPIRED_MESSAGE

        from app.scrapers.auth import expire_litres_session

        _set_user(db, _session_data(), listened=(100,))
        expire_litres_session(db, 1)  # as done by a background sync thread
        assert (
            mw_app.get("/page").get_data(as_text=True)
            == f"EXPIRED|{AUTH_EXPIRED_MESSAGE}"
        )
        assert EXPIRED_NOTICE_KEY not in _user_state(db)[0]
        assert mw_app.get("/page").get_data(as_text=True) == "EXPIRED|"

    def test_per_user_staleness_ignores_other_users_sync_runs(self):
        from app.middleware import _profile_sync_is_stale

        assert _profile_sync_is_stale({}, 20) is True
        assert (
            _profile_sync_is_stale(
                {PROFILE_SYNCED_AT_KEY: _iso(-timedelta(hours=21))}, 20
            )
            is True
        )
        assert (
            _profile_sync_is_stale(
                {PROFILE_SYNCED_AT_KEY: _iso(-timedelta(hours=1))}, 20
            )
            is False
        )


@pytest.fixture
def auth_client(db):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    with (
        patch("app.middleware.SessionLocal", db),
        patch("app.controllers.auth.SessionLocal", db),
        patch("app.controllers.auth.run_profile"),
    ):
        from app.controllers.auth import bp
        from app.middleware import register_session_middleware

        register_session_middleware(app)
        app.register_blueprint(bp)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
        yield client


def _expire(db, listened=(100, 200)):
    from app.scrapers.auth import expire_litres_session

    _set_user(db, _session_data(), listened=listened)
    expire_litres_session(db, 1)


class TestExpiredAccountFlows:
    def test_expired_account_still_gates_filters_on(self, db):
        """(1) catalog gates hide-listened on _is_authenticated → stays enabled."""
        from app.controllers.auth import _is_authenticated

        _expire(db)
        assert _is_authenticated(db, 1) == (True, EMAIL)

    def test_status_reports_expired(self, auth_client, db):
        _expire(db)
        body = auth_client.get("/auth/status").get_json()
        assert body == {
            "authenticated": True,
            "email": EMAIL,
            "expired": True,
            "listened_count": 2,
        }

    @patch("app.controllers.auth.get_valid_token")
    def test_login_passes_force_login(self, mock_token, auth_client):
        mock_token.return_value = "jwt"
        resp = auth_client.post("/auth/login", json={"email": EMAIL, "password": "pw"})
        assert resp.status_code == 200
        assert mock_token.call_args.kwargs["force_login"] is True

    @patch("app.controllers.auth.get_valid_token")
    def test_relogin_same_email_keeps_listened(self, mock_token, auth_client, db):
        """(3) same account (case-insensitive) resumes; sync refreshes the list."""
        _expire(db)
        mock_token.return_value = "jwt"
        resp = auth_client.post(
            "/auth/login", json={"email": EMAIL.upper(), "password": "pw"}
        )
        assert resp.get_json()["listened_count"] == 2

    @patch("app.controllers.auth.get_valid_token")
    def test_login_different_email_replaces_listened(self, mock_token, auth_client, db):
        _expire(db)
        mock_token.return_value = "jwt"
        resp = auth_client.post(
            "/auth/login", json={"email": "other@example.com", "password": "pw"}
        )
        assert resp.get_json()["listened_count"] == 0
        assert _user_state(db)[1] == "other@example.com"

    @patch("app.controllers.auth.get_valid_token")
    def test_failed_relogin_keeps_expired_state(self, mock_token, auth_client, db):
        _expire(db)
        mock_token.side_effect = LitresAuthRejectedError("wrong email or password")
        resp = auth_client.post(
            "/auth/login", json={"email": "other@example.com", "password": "bad"}
        )
        assert resp.status_code == 401
        data, login, listened = _user_state(db)
        assert data[AUTH_STATE_KEY] == "expired" and login == EMAIL and listened == 2

    def test_manual_logout_still_clears_everything(self, auth_client, db):
        _expire(db)
        assert auth_client.post("/auth/logout").status_code == 200
        data, login, listened = _user_state(db)
        assert data is None and login is None and listened == 0
        assert auth_client.get("/auth/status").get_json()["authenticated"] is False


# ---------------------------------------------------------------------------
# Step 4: password-free renewal via the long-lived LitRes session id
# ---------------------------------------------------------------------------

SID = "s" * 32


def _session_data_with_sid(**kw) -> str:
    data = json.loads(_session_data(**kw))
    data["sid"] = SID
    return json.dumps(data)


class TestSessionIdAuth:
    def test_auth_headers(self):
        from app.scrapers.profile import auth_headers

        assert auth_headers("jwt", SID) == {"authorization": "Bearer jwt"}
        assert auth_headers(None, SID) == {"session-id": SID}
        with pytest.raises(ValueError):
            auth_headers(None, None)

    def test_fetch_with_sid_sends_session_id_header(self):
        from app.scrapers.profile import fetch_finished_book_ids

        client = MagicMock()
        client.get.return_value = _resp(
            200, {"payload": {"data": [{"id": 100}], "pagination": {}}}
        )
        assert fetch_finished_book_ids(client, sid=SID) == [100]
        headers = client.get.call_args.kwargs["headers"]
        assert headers["session-id"] == SID
        assert "authorization" not in headers

    def test_session_invalid_403_is_auth_error(self):
        from app.scrapers.profile import LitresProfileAuthError, fetch_finished_book_ids

        client = MagicMock()
        client.get.return_value = _resp(
            403,
            {
                "status": 403,
                "error": {"type": "PermissionMissing", "code": "SESSION_INVALID"},
            },
        )
        with pytest.raises(LitresProfileAuthError, match="403"):
            fetch_finished_book_ids(client, sid=SID)

    @patch("app.scrapers.auth.litres_login")
    def test_login_persists_sid(self, mock_login, db):
        from app.scrapers.auth import get_valid_token

        mock_login.return_value = AuthResult("jwt", "r", 86400, sid=SID)
        get_valid_token(
            session_factory=db, user_id=1, email=EMAIL, password="pw", force_login=True
        )
        assert _user_state(db)[0]["sid"] == SID


class TestRunProfileSessionId:
    @patch("app.sync.profile.fetch_finished_book_ids")
    @patch("app.sync.profile.get_valid_token")
    def test_expired_jwt_uses_sid_without_password(
        self, mock_token, mock_fetch, db, no_env_creds
    ):
        from app.sync.profile import run_profile

        _set_user(db, _session_data_with_sid(token_ttl=timedelta(days=-2)))
        mock_fetch.return_value = [100, 200]
        run_profile(
            session_factory=db, email=EMAIL, user_id=1, expire_on_auth_failure=True
        )

        mock_token.assert_not_called()  # no refresh / login attempted
        assert mock_fetch.call_args.kwargs == {"sid": SID}
        data, _, listened = _user_state(db)
        assert listened == 2
        assert data["access_token"] == "jwt"  # session stays active
        assert PROFILE_SYNCED_AT_KEY in data

    @patch("app.sync.profile.fetch_finished_book_ids")
    @patch("app.sync.profile.get_valid_token")
    def test_rejected_jwt_falls_back_to_sid(self, mock_token, mock_fetch, db):
        from app.scrapers.profile import LitresProfileAuthError
        from app.sync.profile import run_profile

        _set_user(db, _session_data_with_sid())
        mock_token.return_value = "jwt"
        mock_fetch.side_effect = [LitresProfileAuthError("401"), [100]]
        run_profile(session_factory=db, email=EMAIL, user_id=1)

        assert mock_fetch.call_args_list[0].kwargs == {"access_token": "jwt"}
        assert mock_fetch.call_args_list[1].kwargs == {"sid": SID}
        assert mock_token.call_count == 1

    @patch("app.sync.profile.fetch_finished_book_ids")
    @patch("app.sync.profile.get_valid_token")
    def test_sid_rejected_and_no_password_expires_keeping_data(
        self, mock_token, mock_fetch, db, no_env_creds
    ):
        from app.scrapers.profile import LitresProfileAuthError
        from app.sync.profile import run_profile

        _set_user(
            db, _session_data_with_sid(token_ttl=timedelta(days=-2)), listened=(100,)
        )
        mock_fetch.side_effect = LitresProfileAuthError("403 SESSION_INVALID")
        mock_token.side_effect = LitresAuthRejectedError("no password")
        with pytest.raises(LitresAuthRejectedError):
            run_profile(
                session_factory=db, email=EMAIL, user_id=1, expire_on_auth_failure=True
            )
        assert mock_token.call_args.kwargs["force_renew"] is True
        data, login, listened = _user_state(db)
        assert data[AUTH_STATE_KEY] == "expired" and "sid" not in data
        assert login == EMAIL and listened == 1

    @patch("app.middleware.threading.Thread")
    @patch("app.middleware.get_valid_token")
    def test_middleware_trusts_stored_sid_without_network(
        self, mock_token, mock_thread, mw_app, db, no_env_creds
    ):
        _set_user(
            db,
            _session_data_with_sid(
                token_ttl=timedelta(days=-2), synced_ago=timedelta(days=3)
            ),
        )
        assert mw_app.get("/page").get_data(as_text=True) == "IN|"
        mock_token.assert_not_called()
        mock_thread.assert_called_once()
