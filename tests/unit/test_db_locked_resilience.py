"""Tests for the database-is-locked resilience fixes.

Covers:
- ``commit_with_retry`` retry/backoff behavior on SQLite lock errors
- ``reap_zombie_sync_runs`` returns reaped count and is idempotent
- ``finalise_sync_run`` writes the terminal state in a fresh session
- App startup invokes the zombie reaper
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import SyncConfig, SyncRun


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


def _make_locked_error(msg: str = "database is locked") -> OperationalError:
    return OperationalError("UPDATE x", {}, Exception(msg))


# =========================================================================
# commit_with_retry
# =========================================================================


class TestCommitWithRetry:
    def test_success_on_first_attempt(self):
        from app.sync.common import commit_with_retry

        session = MagicMock()
        commit_with_retry(session, attempts=3, base_delay=0.0)

        session.commit.assert_called_once()
        session.rollback.assert_not_called()

    def test_retries_on_lock_error_then_succeeds(self):
        from app.sync.common import commit_with_retry

        session = MagicMock()
        session.commit.side_effect = [
            _make_locked_error(),
            _make_locked_error(),
            None,  # success on 3rd
        ]

        commit_with_retry(session, attempts=5, base_delay=0.0)

        assert session.commit.call_count == 3
        assert session.rollback.call_count == 2

    def test_raises_after_exhausting_attempts(self):
        from app.sync.common import commit_with_retry

        session = MagicMock()
        session.commit.side_effect = _make_locked_error()

        with pytest.raises(OperationalError, match="database is locked"):
            commit_with_retry(session, attempts=3, base_delay=0.0)

        assert session.commit.call_count == 3
        assert session.rollback.call_count == 3  # rollback after each failed attempt

    def test_does_not_retry_non_lock_operational_error(self):
        from app.sync.common import commit_with_retry

        session = MagicMock()
        session.commit.side_effect = OperationalError(
            "UPDATE x", {}, Exception("disk I/O error")
        )

        with pytest.raises(OperationalError, match="disk I/O error"):
            commit_with_retry(session, attempts=5, base_delay=0.0)

        assert session.commit.call_count == 1  # no retry
        session.rollback.assert_not_called()

    def test_does_not_retry_unrelated_exception(self):
        from app.sync.common import commit_with_retry

        session = MagicMock()
        session.commit.side_effect = ValueError("not a DB error")

        with pytest.raises(ValueError):
            commit_with_retry(session, attempts=5, base_delay=0.0)

        assert session.commit.call_count == 1


# =========================================================================
# reap_zombie_sync_runs
# =========================================================================


class TestReapZombieSyncRuns:
    def test_returns_zero_when_no_zombies(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        from app.sync.common import reap_zombie_sync_runs

        with factory() as s:
            assert reap_zombie_sync_runs(s) == 0

    def test_reaps_run_with_finished_at_set(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                finished_at=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import reap_zombie_sync_runs

        with factory() as s:
            count = reap_zombie_sync_runs(s)
            assert count == 1
            run = s.query(SyncRun).first()
            assert run.status == "failed"
            assert "finished_at" in run.error_message

    def test_reaps_run_older_than_timeout(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime.now(timezone.utc) - timedelta(hours=25),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import reap_zombie_sync_runs

        with factory() as s:
            count = reap_zombie_sync_runs(s)
            assert count == 1
            assert s.query(SyncRun).first().status == "failed"

    def test_idempotent_second_call_reaps_nothing(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                finished_at=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import reap_zombie_sync_runs

        with factory() as s:
            assert reap_zombie_sync_runs(s) == 1
        with factory() as s:
            assert reap_zombie_sync_runs(s) == 0

    def test_does_not_reap_genuinely_running_recent_run(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.sync.common import reap_zombie_sync_runs

        with factory() as s:
            assert reap_zombie_sync_runs(s) == 0
            assert s.query(SyncRun).first().status == "running"


# =========================================================================
# finalise_sync_run
# =========================================================================


class TestFinaliseSyncRun:
    def test_writes_terminal_status_and_error(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            run = SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime.now(timezone.utc),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            )
            s.add(run)
            s.commit()
            run_id = run.id

        from app.sync.common import finalise_sync_run

        with patch("app.db.SessionLocal", factory):
            finalise_sync_run(
                run_id,
                status="failed",
                error_message="boom",
                extra={"books_new": 3, "books_updated": 4, "books_failed": 1},
            )

        with factory() as s:
            run = s.query(SyncRun).filter_by(id=run_id).one()
            assert run.status == "failed"
            assert run.error_message == "boom"
            assert run.finished_at is not None
            assert run.books_new == 3
            assert run.books_updated == 4
            assert run.books_failed == 1

    def test_writes_done_status_with_no_error(self):
        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            run = SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime.now(timezone.utc),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            )
            s.add(run)
            s.commit()
            run_id = run.id

        from app.sync.common import finalise_sync_run

        with patch("app.db.SessionLocal", factory):
            finalise_sync_run(run_id, status="done", error_message=None)

        with factory() as s:
            run = s.query(SyncRun).filter_by(id=run_id).one()
            assert run.status == "done"
            assert run.error_message is None
            assert run.finished_at is not None


# =========================================================================
# Boot-time zombie reaper
# =========================================================================


class TestBootReaper:
    def test_create_app_invokes_reaper(self):
        with patch("app.start._reap_zombie_sync_runs_on_boot") as mock_reap:
            from app.start import create_app

            create_app()
            mock_reap.assert_called_once()

    def test_reaper_logs_when_zombies_present(self, caplog):
        import logging

        factory = _make_factory()
        _seed_sync_config(factory)

        with factory() as s:
            s.add(SyncRun(
                sync_config_id=1, type="bulk", status="running",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                finished_at=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                pages_fetched=0, books_upserted=0, series_fetched=0,
            ))
            s.commit()

        from app.start import _reap_zombie_sync_runs_on_boot

        with patch("app.db.SessionLocal", factory), caplog.at_level(logging.INFO):
            _reap_zombie_sync_runs_on_boot()

        assert any("reaped 1 zombie" in rec.message for rec in caplog.records)

    def test_reaper_swallows_errors(self, caplog):
        import logging

        from app.start import _reap_zombie_sync_runs_on_boot

        # Force an error by patching SessionLocal to raise
        broken = MagicMock(side_effect=RuntimeError("db unreachable"))
        with patch("app.db.SessionLocal", broken), caplog.at_level(logging.ERROR):
            # Must not raise — boot is best-effort
            _reap_zombie_sync_runs_on_boot()

        assert any("zombie sync_run reaper failed" in rec.message for rec in caplog.records)


# =========================================================================
# End-to-end: bulk crash → status='failed'
# =========================================================================


class TestBulkCrashFinalisation:
    """Regression: a mid-loop exception during bulk sync must persist
    status='failed' and a populated error_message to the sync_run row.
    The pre-fix code lost the status="failed" assignment when the session
    rollback re-fetched the run from a DB that still had status='running'.
    """

    def test_lock_error_during_page_commit_marks_run_failed(self, tmp_path):
        from app.sync import bulk

        factory = _make_factory()
        _seed_sync_config(factory)

        # Mock fetch_catalog_page to return one page with no books
        # (so the loop is a no-op except for the page commit).
        page_stub = MagicMock(books=[], next_offset=None)

        # Force the per-page commit to fail with a lock error after retries.
        def commit_with_retry_stub(session, **kwargs):
            raise _make_locked_error()

        # Patch SessionLocal so run_bulk uses the in-memory test DB everywhere
        # (open_sync_run, get_sync_config, finalise_sync_run all use it).
        with (
            patch("app.sync.bulk.SessionLocal", factory),
            patch("app.db.SessionLocal", factory),
            patch("app.sync.bulk.fetch_catalog_page", return_value=page_stub),
            patch("app.sync.bulk.RateLimitedClient") as mock_client,
            patch("app.config.config.Config.SYNC_DIR", str(tmp_path)),
            # Make the empty-page branch trigger a commit-with-retry to hit our stub.
            patch("app.sync.bulk.commit_with_retry", side_effect=commit_with_retry_stub),
        ):
            mock_client.return_value.__enter__ = lambda self: MagicMock()
            mock_client.return_value.__exit__ = lambda *a: None
            # An empty page sets final_status='done' before the post-loop commit.
            # The post-loop commit_with_retry then raises → caught by except Exception
            # → final_status='failed' → finalise_sync_run writes 'failed'.
            with pytest.raises(OperationalError):
                bulk.run_bulk(max_pages=1)

        # Verify the sync_run row landed in 'failed' status with a populated error.
        with factory() as s:
            run = s.query(SyncRun).order_by(SyncRun.id.desc()).first()
            assert run is not None
            assert run.status == "failed", (
                f"Expected status='failed' but got {run.status!r} "
                f"(this is the regression — pre-fix code lost the status update)"
            )
            assert run.error_message and "locked" in run.error_message.lower()
            assert run.finished_at is not None
