# app/sync/profile.py
"""Profile sync engine — fetch listened books from LitRes user profile.

Uses get_valid_token() for smart auth (stored token → refresh → re-login),
falls back to the long-lived LitRes session id (Session-Id header) when the
JWT is expired or rejected, and records per-user profile_synced_at on success.
Fetches finished book IDs, upserts into user_listened_book.
Uses same SyncRun tracking as bulk.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.db.models import Book, SyncRun, User, UserListenedBook
from app.scrapers.auth import (
    PROFILE_SYNC_CLAIM_KEY,
    PROFILE_SYNCED_AT_KEY,
    LitresAuthRejectedError,
    _load_stored_tokens,
    access_token_valid,
    expire_litres_session,
    get_valid_token,
    has_litres_tokens,
    parse_session_data,
)
from app.scrapers.profile import LitresProfileAuthError, fetch_finished_book_ids
from app.sync.common import get_sync_config

log = logging.getLogger(__name__)


class ProfileSyncAccountChanged(Exception):
    """The account was logged out or switched while the sync was fetching."""


def _parse_utc(raw) -> datetime | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def profile_sync_is_stale(data: dict, stale_hours: int, now: datetime | None = None) -> bool:
    """Per-user staleness from session_data['profile_synced_at'] (missing → stale)."""
    synced_at = _parse_utc(data.get(PROFILE_SYNCED_AT_KEY))
    if synced_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - synced_at).total_seconds() / 3600 >= stale_hours


def profile_sync_claim_active(data: dict, retry_minutes: int, now: datetime | None = None) -> bool:
    """True while a previous claim (started/failed sync) still blocks a new one."""
    claimed_at = _parse_utc(data.get(PROFILE_SYNC_CLAIM_KEY))
    if claimed_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - claimed_at < timedelta(minutes=retry_minutes)


def claim_profile_sync(
    session_factory,
    user_id: int,
    *,
    retry_minutes: int,
    stale_hours: int | None = None,
    require_tokens: bool = True,
    now: datetime | None = None,
) -> bool:
    """Atomically claim the right to start a profile sync for `user_id`.

    The claim is a timestamp in User.session_data written with a
    compare-and-swap UPDATE, so it is shared by every gunicorn worker and
    cannot be bypassed by replaying an old cookie (audit W6). A claim blocks
    further claims for `retry_minutes`, which also backs off retries after a
    failed or transiently unreachable LitRes (audit W12).

    `require_tokens=False` is for the login endpoint, which has just
    authenticated the account itself.

    Returns True if this caller won the claim and should start the sync.
    Returns False when not logged in, not stale (if `stale_hours` given),
    claimed recently, lost the race, or the DB is busy.
    """
    now = now or datetime.now(timezone.utc)
    try:
        with session_factory() as s:
            row = s.execute(select(User.session_data).where(User.id == user_id)).first()
            if row is None:
                return False
            raw = row[0]
            data = parse_session_data(raw)
            if require_tokens and not has_litres_tokens(data):
                return False
            if stale_hours is not None and not profile_sync_is_stale(data, stale_hours, now):
                return False
            if profile_sync_claim_active(data, retry_minutes, now):
                return False
            data[PROFILE_SYNC_CLAIM_KEY] = now.isoformat()
            result = s.execute(
                update(User)
                .where(
                    User.id == user_id,
                    User.session_data.is_(None) if raw is None else User.session_data == raw,
                )
                .values(session_data=json.dumps(data))
            )
            s.commit()
            return result.rowcount == 1
    except OperationalError as exc:
        log.warning("Profile sync claim for user %d skipped (DB busy): %s", user_id, exc)
        return False


def _check_account_unchanged(session, user_id: int, email: str) -> dict:
    """Re-read the user inside the write transaction; raise if the account changed.

    Call only after the transaction holds the SQLite write lock (i.e. after a
    flush of pending writes): then no logout/login can commit until ours does.
    """
    row = session.execute(
        select(User.session_data, User.litres_login).where(User.id == user_id)
    ).first()
    data = parse_session_data(row[0]) if row else {}
    current = (data.get("email") or "").strip().lower()
    if not has_litres_tokens(data) or current != (email or "").strip().lower():
        raise ProfileSyncAccountChanged(
            f"Account for user {user_id} was logged out or switched during the sync"
        )
    return data


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
            # The flush takes the SQLite write lock; from here until commit no
            # logout or account switch can commit. Only then verify that the
            # account the books were fetched for is still the linked one, so a
            # logout/switch during the fetch cannot resurrect or mix the
            # listened list (audit W4/W18).
            session.flush()
            data = _check_account_unchanged(session, user_id, email)
            data[PROFILE_SYNCED_AT_KEY] = run.finished_at.isoformat()
            data.pop(PROFILE_SYNC_CLAIM_KEY, None)
            session.execute(
                update(User).where(User.id == user_id).values(session_data=json.dumps(data))
            )
            session.commit()

            log.info(
                "Profile sync done: %d new, %d already known, %d skipped (not in DB)",
                len(new_ids), len(already_listened), skipped,
            )

        except ProfileSyncAccountChanged as exc:
            session.rollback()
            log.info("Profile sync discarded: %s", exc)
            run.status = "failed"
            run.books_upserted = 0
            run.finished_at = datetime.now(timezone.utc)
            run.error_message = str(exc)[:500]
            session.commit()
        except Exception as exc:
            session.rollback()
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.error_message = str(exc)[:500]
            session.commit()
            if expire_on_auth_failure and isinstance(exc, LitresAuthRejectedError):
                # Only expire the account this sync was for — never one the
                # user switched to meanwhile.
                expire_litres_session(session_factory, user_id, email=email)
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
