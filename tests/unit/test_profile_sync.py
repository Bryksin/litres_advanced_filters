# tests/unit/test_profile_sync.py
"""Unit tests for profile sync engine — mocked scrapers, in-memory DB."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Book, SyncConfig, SyncRun, User, UserListenedBook


@pytest.fixture
def profile_db():
    """In-memory SQLite with schema + SyncConfig + User + some books."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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
        s.add(User(id=1, created_at=datetime.now(timezone.utc)))
        # Add some books that exist in our DB
        for bid in [100, 200, 300, 400, 500]:
            s.add(Book(
                id=bid,
                title=f"Book {bid}",
                url=f"/book/{bid}",
                art_type=1,
                cached_at=datetime.now(timezone.utc),
            ))
        s.commit()

    return Factory


class TestRunProfile:
    @patch("app.sync.profile.get_valid_token")
    @patch("app.sync.profile.fetch_finished_book_ids")
    def test_upserts_listened_books(self, mock_fetch, mock_get_token, profile_db):
        from app.sync.profile import run_profile

        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = [100, 200, 300]

        run_profile(
            session_factory=profile_db,
            email="test@example.com",
            password="pass",
            user_id=1,
        )

        with profile_db() as session:
            listened = session.query(UserListenedBook).all()
            assert len(listened) == 3
            listened_ids = {row.book_id for row in listened}
            assert listened_ids == {100, 200, 300}

    @patch("app.sync.profile.get_valid_token")
    @patch("app.sync.profile.fetch_finished_book_ids")
    def test_skips_unknown_book_ids(self, mock_fetch, mock_get_token, profile_db):
        """Book IDs not in our DB should be skipped (not crash)."""
        from app.sync.profile import run_profile

        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = [100, 999999]  # 999999 not in DB

        run_profile(
            session_factory=profile_db,
            email="test@example.com",
            password="pass",
            user_id=1,
        )

        with profile_db() as session:
            listened = session.query(UserListenedBook).all()
            assert len(listened) == 1
            assert listened[0].book_id == 100

    @patch("app.sync.profile.get_valid_token")
    @patch("app.sync.profile.fetch_finished_book_ids")
    def test_idempotent_upsert(self, mock_fetch, mock_get_token, profile_db):
        """Running profile sync twice should not create duplicates."""
        from app.sync.profile import run_profile

        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = [100, 200]

        run_profile(session_factory=profile_db, email="t@e.com", password="p", user_id=1)
        run_profile(session_factory=profile_db, email="t@e.com", password="p", user_id=1)

        with profile_db() as session:
            listened = session.query(UserListenedBook).all()
            assert len(listened) == 2

    @patch("app.sync.profile.get_valid_token")
    @patch("app.sync.profile.fetch_finished_book_ids")
    def test_creates_sync_run_record(self, mock_fetch, mock_get_token, profile_db):
        from app.sync.profile import run_profile

        mock_get_token.return_value = "fake-token"
        mock_fetch.return_value = [100]

        run_profile(session_factory=profile_db, email="t@e.com", password="p", user_id=1)

        with profile_db() as session:
            runs = session.query(SyncRun).filter_by(type="profile").all()
            assert len(runs) == 1
            assert runs[0].status == "done"
            assert runs[0].books_upserted == 1

    @patch("app.sync.profile.get_valid_token")
    def test_auth_failure_marks_run_failed(self, mock_get_token, profile_db):
        from app.scrapers.auth import LitresAuthError
        from app.sync.profile import run_profile

        mock_get_token.side_effect = LitresAuthError("wrong password")

        with pytest.raises(LitresAuthError):
            run_profile(session_factory=profile_db, email="t@e.com", password="p", user_id=1)

        with profile_db() as session:
            runs = session.query(SyncRun).filter_by(type="profile").all()
            assert len(runs) == 1
            assert runs[0].status == "failed"
