"""Shared helpers for sync engines."""

import json
import logging
import os
import traceback
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Genre, SyncConfig, SyncRun
from app.scrapers.models import Art

log = logging.getLogger(__name__)

GENRE_ID = 201583
GENRE_SLUG = "legkoe-chtenie"
DEFAULT_LIMIT = 24
RETRY_WAITS = [60, 300, 600]


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


def check_no_running_sync(session: Session) -> None:
    running = session.query(SyncRun).filter_by(status="running").first()
    if running:
        raise RuntimeError(
            f"Sync already running: SyncRun id={running.id} started at {running.started_at}. "
            "If the process crashed, manually update its status to 'failed' to unblock."
        )


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
