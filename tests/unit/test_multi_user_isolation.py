"""Multi-user isolation tests — prove per-user data separation.

All tests use in-memory SQLite DBs (never the prod DB).
Two separate test_client() instances simulate two different browsers
(each gets its own session cookie -> own User row via middleware).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from flask import Flask, g, jsonify
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import User, UserSettings, UserIgnoredBook, Book


@pytest.fixture
def isolation_app():
    """Flask app with middleware + catalog routes + in-memory DB + seed books."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)

    # Seed books
    with Factory() as s:
        for bid in [1, 2, 3]:
            s.add(Book(
                id=bid, title=f"Book {bid}", url=f"/b/{bid}",
                art_type=1, cached_at=datetime.now(timezone.utc),
            ))
        s.commit()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"

    with patch("app.middleware.SessionLocal", Factory), \
         patch("app.controllers.catalog.SessionLocal", Factory):
        from app.middleware import register_session_middleware
        register_session_middleware(app)

        from app.controllers.catalog import bp
        app.register_blueprint(bp)

        # Helper route to read g.user_id
        @app.route("/test-whoami")
        def whoami():
            return jsonify({"user_id": g.user_id})

        yield app, Factory


class TestIgnoreListIsolation:
    def test_two_users_have_separate_ignore_lists(self, isolation_app):
        """User A ignoring book 1 should not affect User B's ignore list."""
        app, Factory = isolation_app

        # User A: new session -> auto-created user -> ignores book 1
        with app.test_client() as client_a:
            resp = client_a.post("/ignore/1")
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True
            uid_a = client_a.get("/test-whoami").get_json()["user_id"]

        # User B: separate session -> different auto-created user -> ignores book 2
        with app.test_client() as client_b:
            resp = client_b.post("/ignore/2")
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True
            uid_b = client_b.get("/test-whoami").get_json()["user_id"]

        # Different users
        assert uid_a != uid_b

        # Verify isolation in DB
        with Factory() as s:
            a_ignored = [r.book_id for r in s.query(UserIgnoredBook).filter_by(user_id=uid_a).all()]
            b_ignored = [r.book_id for r in s.query(UserIgnoredBook).filter_by(user_id=uid_b).all()]
            assert a_ignored == [1]
            assert b_ignored == [2]

    def test_clear_ignored_only_affects_own_user(self, isolation_app):
        """POST /ignore/clear for User A must not touch User B's ignore list."""
        app, Factory = isolation_app

        # User A ignores book 1
        with app.test_client() as client_a:
            client_a.post("/ignore/1")
            uid_a = client_a.get("/test-whoami").get_json()["user_id"]

        # User B ignores book 2
        with app.test_client() as client_b:
            client_b.post("/ignore/2")
            uid_b = client_b.get("/test-whoami").get_json()["user_id"]

        # User A clears their ignore list (reuse session by setting user_id)
        with app.test_client() as client_a2:
            with client_a2.session_transaction() as sess:
                sess["user_id"] = uid_a
            client_a2.post("/ignore/clear")

        # User B's ignore list should be untouched
        with Factory() as s:
            a_count = s.query(UserIgnoredBook).filter_by(user_id=uid_a).count()
            b_count = s.query(UserIgnoredBook).filter_by(user_id=uid_b).count()
            assert a_count == 0
            assert b_count == 1


class TestSessionIsolation:
    def test_two_browsers_get_different_user_ids(self, isolation_app):
        """Two separate test_client instances (= two browsers) get different user_ids."""
        app, Factory = isolation_app

        with app.test_client() as client_a:
            uid_a = client_a.get("/test-whoami").get_json()["user_id"]
        with app.test_client() as client_b:
            uid_b = client_b.get("/test-whoami").get_json()["user_id"]

        assert uid_a != uid_b

        # Both have their own UserSettings rows
        with Factory() as s:
            assert s.get(UserSettings, uid_a) is not None
            assert s.get(UserSettings, uid_b) is not None
