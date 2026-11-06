# app/sync/profile.py
"""Profile sync engine — fetch listened books from LitRes user profile.

Uses get_valid_token() for smart auth (stored token → refresh → re-login),
falls back to the long-lived LitRes session id (Session-Id header) when the
JWT is expired or rejected, and records per-user profile_synced_at on success.
Fetches finished book IDs, upserts into user_listened_book.
Uses same SyncRun tracking as bulk.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.db.models import Book, SyncRun, UserListenedBook
from app.scrapers.auth import (
    LitresAuthRejectedError,
    _load_stored_tokens,
    access_token_valid,
    expire_litres_session,
    get_valid_token,
    record_profile_synced,
)
from app.scrapers.profile import LitresProfileAuthError, fetch_finished_book_ids
from app.sync.common import get_sync_config

log = logging.getLogger(__name__)


def run_profile(
    *,
    session_factory,
    email: str,
    password: str | None = None,
    user_id: int,
    dry_run: bool = False,
    expire_on_auth_failure: bool = False,
) -> None:
    """Run profile sync: get token → fetch finished books → upsert user_listened_book.

    Args:
        session_factory: SQLAlchemy sessionmaker (callable returning Session).
        email: LitRes account email.
        password: LitRes password (from env var). None if stored tokens should suffice.
        user_id: Local user ID to associate listened books with.
        dry_run: If True, fetch but don't write to DB.
        expire_on_auth_failure: If True and LitRes definitively rejects the
            session (no way to re-authenticate), log the web user out via
            expire_litres_session() so the UI stops showing a dead login.
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
            book_ids = _fetch_with_reauth(
                session_factory=session_factory,
                user_id=user_id,
                email=email,
                password=password,
            )

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
            record_profile_synced(session_factory, user_id, run.finished_at)

            log.info(
                "Profile sync done: %d new, %d already known, %d skipped (not in DB)",
                len(new_ids), len(already_listened), skipped,
            )

        except Exception as exc:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error_message = str(exc)[:500]
            session.commit()
            if expire_on_auth_failure and isinstance(exc, LitresAuthRejectedError):
                expire_litres_session(session_factory, user_id)
            raise


def _fetch_with_reauth(*, session_factory, user_id: int, email: str, password: str | None) -> list[int]:
    """Fetch finished book IDs with the best available credential, renewing as needed.

    Order (verified against LitRes 2026-11-06, see docs/LitRes API Reverse Engineering.md §2.5b):
    1. Still-valid JWT (or, when no sid is stored, whatever get_valid_token yields).
    2. Stored `sid` via the `Session-Id` header — long-lived, needs no password.
    3. get_valid_token(force_renew=True): refresh (currently 404) → re-login with
       a password (explicit or env creds for this email).
    A credential rejected with HTTP 401/403 falls through to the next step.

    Raises:
        LitresAuthRejectedError: Every available credential was rejected / none left.
    """
    with session_factory() as session:
        stored = _load_stored_tokens(session, user_id) or {}
    sid = stored.get("sid")

    def _fetch(**cred) -> list[int]:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            return fetch_finished_book_ids(client, **cred)

    if access_token_valid(stored) or not sid:
        # Get valid token (uses stored → refresh → re-login)
        token = get_valid_token(
            session_factory=session_factory,
            user_id=user_id,
            email=email,
            password=password,
        )
        try:
            return _fetch(access_token=token)
        except LitresProfileAuthError:
            log.warning("LitRes rejected access token — trying stored session id / renewal")

    if sid:
        try:
            ids = _fetch(sid=sid)
            log.info("Profile fetched via stored LitRes session id (JWT expired)")
            return ids
        except LitresProfileAuthError:
            log.warning("LitRes rejected stored session id — trying token renewal")

    token = get_valid_token(
        session_factory=session_factory,
        user_id=user_id,
        email=email,
        password=password,
        force_renew=True,
    )
    try:
        return _fetch(access_token=token)
    except LitresProfileAuthError as exc:
        raise LitresAuthRejectedError(f"LitRes rejected renewed token: {exc}") from exc
