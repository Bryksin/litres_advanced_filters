"""Unit tests for session middleware — user creation and reuse."""

from unittest.mock import patch

import pytest
from flask import Flask, g, jsonify
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Config
from app.db.base import Base
from app.db.models import User, UserSettings
from app.middleware import ANONYMOUS_USER_ID


@pytest.fixture
def mw_app():
    """Flask app with middleware registered and in-memory DB."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"

    # Register middleware
    from app.middleware import register_session_middleware
    with patch("app.middleware.SessionLocal", Factory):
        register_session_middleware(app)

        @app.route("/test-user-id", methods=["GET", "HEAD", "POST"])
        def test_route():
            return jsonify({"user_id": g.user_id})

        yield app, Factory


class TestNewVisitor:
    """Users are created lazily: only a request that stores state writes (W1/W9)."""

    def test_first_get_is_anonymous_and_writes_nothing(self, mw_app):
        app, Factory = mw_app
        with app.test_client() as client:
            resp = client.get("/test-user-id")

        assert resp.status_code == 200
        assert resp.get_json()["user_id"] == ANONYMOUS_USER_ID
        assert "Set-Cookie" not in resp.headers
        with Factory() as s:
            assert s.query(User).count() == 0

    def test_head_and_404_write_nothing(self, mw_app):
        app, Factory = mw_app
        app.test_client().head("/test-user-id")
        app.test_client().get("/robots.txt")
        app.test_client().post("/wp-login.php")
        with Factory() as s:
            assert s.query(User).count() == 0

    def test_first_write_creates_user(self, mw_app):
        app, Factory = mw_app
        with app.test_client() as client:
            resp = client.post("/test-user-id")
            data = resp.get_json()

        assert resp.status_code == 200
        assert isinstance(data["user_id"], int) and data["user_id"] > 0

        # Verify User + UserSettings rows exist, with the unified default genre (Q-S4)
        with Factory() as s:
            user = s.get(User, data["user_id"])
            assert user is not None
            settings = s.get(UserSettings, data["user_id"])
            assert settings is not None
            assert settings.genre_id == Config.DEFAULT_GENRE_ID

    def test_second_visit_reuses_user(self, mw_app):
        app, Factory = mw_app
        with app.test_client() as client:
            resp1 = client.post("/test-user-id")
            uid1 = resp1.get_json()["user_id"]

            resp2 = client.get("/test-user-id")
            uid2 = resp2.get_json()["user_id"]

        assert uid1 == uid2
        with Factory() as s:
            assert s.query(User).count() == 1


class TestInvalidSession:
    def test_deleted_user_gets_new_session(self, mw_app):
        """If user_id in session points to a deleted row, the next write creates a new user."""
        app, Factory = mw_app
        with app.test_client() as client:
            resp1 = client.post("/test-user-id")
            uid1 = resp1.get_json()["user_id"]

            # Create a second user so that the next autoincrement id will differ
            with Factory() as s:
                from datetime import datetime, timezone
                s.add(User(created_at=datetime.now(timezone.utc)))
                s.commit()

            # Delete the first user from DB behind the scenes
            with Factory() as s:
                s.query(UserSettings).filter_by(user_id=uid1).delete()
                s.query(User).filter_by(id=uid1).delete()
                s.commit()

            # A read is anonymous again; the next write gets a fresh user
            assert client.get("/test-user-id").get_json()["user_id"] == ANONYMOUS_USER_ID
            resp2 = client.post("/test-user-id")
            uid2 = resp2.get_json()["user_id"]

        assert uid2 not in (uid1, ANONYMOUS_USER_ID)
        # Verify new user + settings exist
        with Factory() as s:
            assert s.get(User, uid2) is not None
            assert s.get(UserSettings, uid2) is not None


class TestStaticFilesSkipped:
    def test_static_request_skips_middleware(self, mw_app):
        """Requests to /static/ should not trigger user creation."""
        app, Factory = mw_app
        with app.test_client() as client:
            client.get("/static/nonexistent.css")

        # No users should have been created
        with Factory() as s:
            count = s.query(User).count()
            assert count == 0
