# app/sync/profile.py
"""Profile sync engine — fetch listened books from LitRes user profile.

Uses get_valid_token() for smart auth (stored token → refresh → re-login).
Fetches finished book IDs, upserts into user_listened_book.
Uses same SyncRun tracking as bulk.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.db.models import Book, SyncRun, UserListenedBook
from app.scrapers.auth import get_valid_token
from app.scrapers.profile import fetch_finished_book_ids
from app.sync.common import get_sync_config

log = logging.getLogger(__name__)


def run_profile(
    *,
    session_factory,
    email: str,
    password: str | None = None,
    user_id: int,
    dry_run: bool = False,
) -> None:
    """Run profile sync: get token → fetch finished books → upsert user_listened_book.

    Args:
        session_factory: SQLAlchemy sessionmaker (callable returning Session).
        email: LitRes account email.
        password: LitRes password (from env var). None if stored tokens should suffice.
        user_id: Local user ID to associate listened books with.
        dry_run: If True, fetch but don't write to DB.
    """
    with session_factory() as session:
        config = get_sync_config(session)

        run = SyncRun(
            sync_config_id=config.id,
            type="profile",
            started_at=datetime.now(timezone.utc),
            status="running",
            pages_fetched=0,
            books_upserted=0,
            series_fetched=0,
        )
        session.add(run)
        session.commit()

        try:
            # Get valid token (uses stored → refresh → re-login)
            token = get_valid_token(
                session_factory=session_factory,
                user_id=user_id,
                email=email,
                password=password,
            )

            # Fetch finished books with the token
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                book_ids = fetch_finished_book_ids(client, token)

            log.info("Fetched %d finished book IDs from LitRes", len(book_ids))

            if dry_run:
                log.info("Dry run — skipping DB upsert")
                run.status = "done"
                run.books_upserted = 0
                run.finished_at = datetime.now(timezone.utc)
                session.commit()
                return

            # Filter to book IDs that exist in our DB
            existing_ids = {
                row[0]
                for row in session.query(Book.id).filter(Book.id.in_(book_ids)).all()
            } if book_ids else set()

            skipped = len(book_ids) - len(existing_ids)
            if skipped:
                log.info("Skipping %d book IDs not in our DB", skipped)

            # Get already-listened IDs to avoid duplicates
            already_listened = {
                row.book_id
                for row in session.query(UserListenedBook)
                .filter(
                    UserListenedBook.user_id == user_id,
                    UserListenedBook.book_id.in_(existing_ids),
                )
                .all()
            } if existing_ids else set()

            new_ids = existing_ids - already_listened
            now = datetime.now(timezone.utc)

            for bid in new_ids:
                session.add(UserListenedBook(
                    user_id=user_id,
                    book_id=bid,
                    listened_at=now,
                ))

            run.status = "done"
            run.books_upserted = len(new_ids)
            run.finished_at = datetime.now(timezone.utc)
            session.commit()

            log.info(
                "Profile sync done: %d new, %d already known, %d skipped (not in DB)",
                len(new_ids), len(already_listened), skipped,
            )

        except Exception:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
            raise
