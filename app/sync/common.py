"""Shared helpers for sync engines."""

import json
import logging
import os
import random
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import Genre, SyncConfig, SyncRun
from app.scrapers.models import Art

log = logging.getLogger(__name__)

GENRE_ID = 201583
GENRE_SLUG = "legkoe-chtenie"
DEFAULT_LIMIT = 24
RETRY_WAITS = [60, 300, 600]

# Strings that mark a transient SQLite write-lock contention. WAL mode allows
# one writer; under contention SQLAlchemy raises OperationalError with one of
# these messages. Retrying with backoff almost always succeeds.
_LOCK_ERROR_MARKERS = ("database is locked", "database table is locked")
COMMIT_RETRY_ATTEMPTS = 5
COMMIT_RETRY_BASE_DELAY = 0.1


def _is_lock_error(exc: OperationalError) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _LOCK_ERROR_MARKERS)


def commit_with_retry(
    session: Session,
    *,
    attempts: int = COMMIT_RETRY_ATTEMPTS,
    base_delay: float = COMMIT_RETRY_BASE_DELAY,
    context: str = "",
) -> None:
    """Commit ``session`` with bounded retry on SQLite write-lock contention.

    Even with ``PRAGMA busy_timeout=30000`` set, SQLite raises
    ``database is locked`` immediately when two connections that already hold
    read transactions both try to upgrade to write (``SQLITE_BUSY_SNAPSHOT``).
    The busy_handler does not cover that case, so a long-running sync sharing
    the DB with gunicorn workers can crash on a single contended commit.

    Retries only on lock errors; any other ``OperationalError`` (or other
    exception class) is re-raised on the first attempt.

    The caller is responsible for ensuring the session's pending state is
    re-buildable across retries — for per-page bulk sync commits this holds
    because each iteration's writes are self-contained.
    """
    last_exc: OperationalError | None = None
    for i in range(attempts):
        try:
            session.commit()
            if i > 0:
                log.info(
                    "commit_with_retry%s: succeeded after %d retries",
                    f" [{context}]" if context else "",
                    i,
                )
            return
        except OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            last_exc = exc
            session.rollback()
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i) + random.random() * base_delay
            log.warning(
                "commit_with_retry%s: locked, retry %d/%d in %.2fs",
                f" [{context}]" if context else "",
                i + 1,
                attempts,
                delay,
            )
            time.sleep(delay)
    log.error(
        "commit_with_retry%s: giving up after %d attempts",
        f" [{context}]" if context else "",
        attempts,
    )
    raise last_exc  # type: ignore[misc]


def make_run_tag() -> str:
    """Generate a timestamp tag for this sync run (used in log/data filenames)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def ensure_genre(session: Session, genre_ref) -> str | None:
    """Ensure genre exists in DB. Auto-inserts if missing and has a name.

    Returns genre_id (str) if genre is usable, None if skipped.
    """
    genre_id = str(genre_ref.id)
    if session.get(Genre, genre_id) is not None:
        return genre_id

    genre_name = getattr(genre_ref, "name", "") or ""
    if not genre_name:
        return None

    genre_url = getattr(genre_ref, "url", "") or ""
    slug = genre_url.strip("/").split("/")[-1] if genre_url else f"genre-{genre_id}"
    session.add(Genre(
        id=genre_id,
        parent_id=None,
        name=genre_name,
        slug=slug,
        url=genre_url if genre_url else f"/genre/{slug}/",
    ))
    session.flush()
    log.info("Auto-inserted genre_id=%s name=%r", genre_id, genre_name)
    return genre_id


def get_sync_config(session: Session) -> SyncConfig:
    config = session.query(SyncConfig).first()
    if config is None:
        raise RuntimeError(
            "sync_config row not found. Run 'python -m app.sync genres' first to seed the DB."
        )
    return config


def reap_zombie_sync_runs(session: Session, *, timeout_seconds: int = 24 * 3600) -> int:
    """Mark crashed-but-still-'running' sync_run rows as failed.

    A row is considered a zombie if either:
      - ``finished_at`` is populated (process set it but never updated status), or
      - ``started_at`` is older than ``timeout_seconds`` (process is gone).

    Idempotent and safe to call from multiple entry points (app startup,
    pre-sync check). Returns the number of rows reaped.
    """
    running_rows = session.query(SyncRun).filter_by(status="running").all()
    now = datetime.now(timezone.utc)
    reaped = 0

    for run in running_rows:
        if run.finished_at is not None:
            run.status = "failed"
            run.error_message = (
                "Auto-recovered: process crashed after setting finished_at"
            )
            log.warning(
                "Reaped zombie SyncRun id=%d: finished_at was set but status was still 'running'",
                run.id,
            )
            reaped += 1
        elif run.started_at and (now - run.started_at).total_seconds() > timeout_seconds:
            run.status = "failed"
            run.error_message = f"Auto-recovered: sync exceeded {timeout_seconds // 3600}h timeout"
            log.warning(
                "Reaped zombie SyncRun id=%d: started_at=%s exceeded %dh timeout",
                run.id,
                run.started_at,
                timeout_seconds // 3600,
            )
            reaped += 1

    if reaped:
        session.commit()
    return reaped


def check_no_running_sync(session: Session) -> None:
    reap_zombie_sync_runs(session)

    still_running = session.query(SyncRun).filter_by(status="running").first()
    if still_running:
        raise RuntimeError(
            f"Sync already running: SyncRun id={still_running.id} started at {still_running.started_at}. "
            "If the process crashed, manually update its status to 'failed' to unblock."
        )


def finalise_sync_run(
    run_id: int,
    *,
    status: str,
    error_message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write the terminal state of a sync_run in a brand-new session.

    Decoupled from the ingest session so a poisoned in-flight transaction
    cannot prevent the cleanup. Called from ``run_bulk``'s finally block.
    """
    from app.db import SessionLocal  # local import to avoid circular deps

    values: dict[str, Any] = {
        "status": status,
        "finished_at": datetime.now(timezone.utc),
        "error_message": error_message,
    }
    if extra:
        values.update(extra)

    with SessionLocal() as s:
        commit_with_retry(
            _bind_update(s, run_id, values),
            context=f"finalise_sync_run id={run_id}",
        )


def _bind_update(session: Session, run_id: int, values: dict[str, Any]) -> Session:
    """Stage an UPDATE on sync_run; commit happens via commit_with_retry."""
    session.execute(update(SyncRun).where(SyncRun.id == run_id).values(**values))
    return session


def failed_books_path(sync_dir: str, run_tag: str) -> str:
    """Return path for this run's failed_books file."""
    return os.path.join(sync_dir, f"failed_books_{run_tag}.jsonl")


def log_failed_book(
    sync_dir: str,
    run_tag: str,
    art: Art,
    page_number: int,
    exc: Exception,
) -> None:
    os.makedirs(sync_dir, exist_ok=True)
    path = failed_books_path(sync_dir, run_tag)
    record = {
        "book_id": art.id,
        "title": art.title,
        "url": art.url,
        "page_number": page_number,
        "error": str(exc),
        "traceback": traceback.format_exc(),
        "raw_card": art.raw,
    }
    with open(path, "a", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
        f.write("\n")
    log.error("Failed to ingest book %d '%s': %s", art.id, art.title, exc)
