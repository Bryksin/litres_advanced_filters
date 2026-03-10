"""Unit tests for auth controller endpoints."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import User, UserListenedBook, UserSettings, Book
from app.scrapers.auth import LitresAuthError


@pytest.fixture
def auth_app():
    """Flask test app with auth blueprint, middleware, and in-memory DB."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)

    # Seed user + settings + books
    with Factory() as s:
        s.add(User(id=1, created_at=datetime.now(timezone.utc)))
        s.add(UserSettings(user_id=1))
        for bid in [100, 200, 300]:
            s.add(Book(id=bid, title=f"Book {bid}", url=f"/b/{bid}", art_type=1, cached_at=datetime.now(timezone.utc)))
        s.commit()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"

    with patch("app.middleware.SessionLocal", Factory), \
         patch("app.controllers.auth.SessionLocal", Factory):
        from app.middleware import register_session_middleware
        register_session_middleware(app)

        from app.controllers.auth import bp
        app.register_blueprint(bp)

        # Pre-set session to use seeded user_id=1
        @app.route("/_set_session")
        def _set_session():
            from flask import session
            session["user_id"] = 1
            return "ok"

        yield app, Factory


class TestLoginEndpoint:
    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_login_success(self, mock_token, mock_sync, auth_app):
        app, Factory = auth_app
        mock_token.return_value = "fake-jwt"
        mock_sync.return_value = None

        # Simulate 3 listened books after sync
        with Factory() as s:
            now = datetime.now(timezone.utc)
            for bid in [100, 200, 300]:
                s.add(UserListenedBook(user_id=1, book_id=bid, listened_at=now))
            s.commit()

        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.post("/auth/login", json={
                "email": "test@example.com",
                "password": "pass123",
            })
            data = resp.get_json()

        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["listened_count"] == 3
        assert data["email"] == "test@example.com"

    @patch("app.controllers.auth.get_valid_token")
    def test_login_wrong_credentials(self, mock_token, auth_app):
        app, Factory = auth_app
        mock_token.side_effect = LitresAuthError("wrong password")

        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.post("/auth/login", json={
                "email": "test@example.com",
                "password": "wrong",
            })
            data = resp.get_json()

        assert resp.status_code == 401
        assert data["ok"] is False
        assert "error" in data

    def test_login_missing_fields(self, auth_app):
        app, _ = auth_app
        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.post("/auth/login", json={"email": "test@example.com"})
            assert resp.status_code == 400

    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_login_succeeds_when_profile_sync_fails(self, mock_token, mock_sync, auth_app):
        """Auth success + profile sync failure should still return ok=True."""
        app, Factory = auth_app
        mock_token.return_value = "fake-jwt"
        mock_sync.side_effect = RuntimeError("API returned 422")

        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.post("/auth/login", json={
                "email": "test@example.com",
                "password": "pass123",
            })
            data = resp.get_json()

        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["email"] == "test@example.com"
        assert "warning" in data

    @patch("app.controllers.auth.run_profile")
    @patch("app.controllers.auth.get_valid_token")
    def test_login_sets_litres_login(self, mock_token, mock_sync, auth_app):
        app, Factory = auth_app
        mock_token.return_value = "fake-jwt"
        mock_sync.return_value = None

        with app.test_client() as client:
            client.get("/_set_session")
            client.post("/auth/login", json={"email": "user@test.com", "password": "pass"})

        with Factory() as s:
            user = s.get(User, 1)
            assert user.litres_login == "user@test.com"


class TestLogoutEndpoint:
    def test_logout_clears_session(self, auth_app):
        app, Factory = auth_app

        # Pre-set session data
        with Factory() as s:
            user = s.get(User, 1)
            user.session_data = json.dumps({"access_token": "old-jwt"})
            s.commit()

        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.post("/auth/logout")
            data = resp.get_json()

        assert resp.status_code == 200
        assert data["ok"] is True

        with Factory() as s:
            user = s.get(User, 1)
            assert user.session_data is None

    def test_logout_clears_listened_books(self, auth_app):
        app, Factory = auth_app
        with Factory() as s:
            user = s.get(User, 1)
            user.session_data = json.dumps({"access_token": "jwt", "refresh_token": "r"})
            user.litres_login = "test@example.com"
            now = datetime.now(timezone.utc)
            s.add(UserListenedBook(user_id=1, book_id=100, listened_at=now))
            s.commit()

        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.post("/auth/logout")
            assert resp.get_json()["ok"] is True

        with Factory() as s:
            user = s.get(User, 1)
            assert user.session_data is None
            assert user.litres_login is None
            assert s.query(UserListenedBook).filter_by(user_id=1).count() == 0


class TestStatusEndpoint:
    def test_status_authenticated(self, auth_app):
        app, Factory = auth_app

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with Factory() as s:
            user = s.get(User, 1)
            user.session_data = json.dumps({
                "email": "user@example.com",
                "access_token": "valid-jwt",
                "refresh_token": "refresh",
                "expires_at": future,
            })
            s.commit()

        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.get("/auth/status")
            data = resp.get_json()

        assert data["authenticated"] is True
        assert data["email"] == "user@example.com"

    def test_status_not_authenticated(self, auth_app):
        app, _ = auth_app
        with app.test_client() as client:
            client.get("/_set_session")
            resp = client.get("/auth/status")
            data = resp.get_json()

        assert data["authenticated"] is False
