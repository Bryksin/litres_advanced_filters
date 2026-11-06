"""Session middleware — ensures every request has a valid g.user_id.

Also handles persistent sessions, background profile sync and LitRes session
expiry: when a stale profile sync is due and the LitRes session cannot be
renewed, the session is marked expired (tokens dropped, listened data kept)
and `auth_notice` (one-shot) / `auth_expired` are exposed to templates.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, g, session

from app.db import SessionLocal
from app.db.models import User, UserSettings
from app.scrapers.auth import (
    AUTH_STATE_EXPIRED,
    EXPIRED_NOTICE_KEY,
    PROFILE_SYNCED_AT_KEY,
    LitresAuthError,
    LitresAuthRejectedError,
    access_token_valid,
    expire_litres_session,
    get_valid_token,
    has_litres_tokens,
    is_litres_session_expired,
    parse_session_data,
    reauth_password_for,
)

# Default genre for new anonymous users (Космическая фантастика).
# Using root genre (201583) would query all 91k books — too slow for first load.
_DEFAULT_USER_GENRE_ID = "5077"

log = logging.getLogger(__name__)

# Paths that should not trigger user creation or profile sync
_SKIP_PREFIXES = ("/static/", "/covers/")


# Russian UI copy shown once after an automatic LitRes logout
AUTH_EXPIRED_MESSAGE = (
    "Сессия ЛитРес истекла, и продлить её автоматически не удалось. "
    "Список прослушанных книг сохранён — войдите снова, чтобы обновить его."
)
_AUTH_NOTICE_SESSION_KEY = "auth_notice"


def _is_authenticated(user: User) -> tuple[bool, str | None, str | None]:
    """Check if user has stored LitRes tokens. Returns (is_auth, email, password)."""
    data = parse_session_data(user.session_data) if user else {}
    if has_litres_tokens(data):
        return True, data.get("email"), None
    return False, None, None


def _profile_sync_is_stale(data: dict, stale_hours: int) -> bool:
    """Per-user staleness from session_data['profile_synced_at'] (missing → stale)."""
    raw = data.get(PROFILE_SYNCED_AT_KEY)
    if not raw:
        return True
    try:
        synced_at = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return True
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - synced_at).total_seconds() / 3600
    return age_hours >= stale_hours


def _set_expired_notice() -> None:
    """Flag the current browser session to show the 'LitRes session expired' message."""
    session[_AUTH_NOTICE_SESSION_KEY] = AUTH_EXPIRED_MESSAGE
    session.pop("is_admin", None)
    session.pop("_profile_sync_started", None)
    g.litres_auth_state = AUTH_STATE_EXPIRED


def _consume_expired_marker(user_id: int) -> None:
    """Turn the one-shot DB notice flag (left by expire_litres_session) into a UI notice.

    Only the flag is removed; the expired state, email and listened data stay.
    """
    with SessionLocal() as s:
        user = s.get(User, user_id)
        if user is None:
            return
        data = parse_session_data(user.session_data)
        if EXPIRED_NOTICE_KEY not in data:
            return
        data.pop(EXPIRED_NOTICE_KEY)
        user.session_data = json.dumps(data)
        s.commit()
    _set_expired_notice()


def _auth_state(data: dict) -> str | None:
    """'active' (live tokens), 'expired' (account known, tokens gone) or None."""
    if has_litres_tokens(data):
        return "active"
    if is_litres_session_expired(data):
        return AUTH_STATE_EXPIRED
    return None


def _litres_session_renewable(user_id: int, email: str, data: dict) -> bool:
    """Make sure a profile sync can authenticate; auto-logout if it never can.

    Cheap paths first (no network): a still-valid access token, a stored
    LitRes session id (long-lived, used via the Session-Id header), or env
    credentials for this email (the background sync will re-login). Otherwise
    try the token refresh synchronously. A definitive rejection logs the user
    out now, so this very page already renders as logged out with a notice.
    Transient failures keep the login and just skip this sync round.
    """
    if access_token_valid(data) or data.get("sid") or reauth_password_for(email):
        # The background sync renews via the stored sid / env creds and marks
        # the session expired itself if LitRes rejects everything.
        return True
    try:
        get_valid_token(session_factory=SessionLocal, user_id=user_id, email=email)
        return True
    except LitresAuthRejectedError as exc:
        log.info("LitRes session for user %d cannot be renewed (%s) — logging out", user_id, exc)
        if expire_litres_session(SessionLocal, user_id):
            _consume_expired_marker(user_id)
        return False
    except LitresAuthError as exc:
        log.warning("LitRes session check for user %d failed transiently: %s", user_id, exc)
        return False


def _maybe_background_profile_sync(user_id: int, email: str, data: dict | None = None) -> None:
    """Trigger background profile sync if this user's last one is stale (>PROFILE_SYNC_STALE_HOURS)."""
    from app.config import Config

    stale_hours = Config.PROFILE_SYNC_STALE_HOURS
    if data is None:
        with SessionLocal() as s:
            user = s.get(User, user_id)
            data = parse_session_data(user.session_data) if user else {}

    if not _profile_sync_is_stale(data, stale_hours):
        return  # Still fresh

    if not _litres_session_renewable(user_id, email, data):
        return

    log.info("Profile sync stale (>%dh) — triggering background sync for user %d", stale_hours, user_id)
    session["_profile_sync_started"] = datetime.now(timezone.utc).isoformat()

    def _run():
        try:
            from app.sync.profile import run_profile
            run_profile(
                session_factory=SessionLocal,
                email=email,
                user_id=user_id,
                expire_on_auth_failure=True,
            )
            log.info("Background profile sync completed for user %d", user_id)
        except Exception:
            log.exception("Background profile sync failed for user %d", user_id)

    t = threading.Thread(target=_run, daemon=True, name=f"profile-sync-{user_id}")
    t.start()


def register_session_middleware(app: Flask) -> None:
    """Register before_request handler that resolves session -> g.user_id."""

    @app.before_request
    def _ensure_user():
        from flask import request

        # Skip for static assets
        if any(request.path.startswith(p) for p in _SKIP_PREFIXES):
            return

        # Make all sessions persistent (cookie survives browser close)
        session.permanent = True

        user_id = session.get("user_id")

        if user_id is not None:
            # Validate user still exists (read what we need, release the DB session)
            with SessionLocal() as s:
                user = s.get(User, user_id)
                exists = user is not None
                data = parse_session_data(user.session_data) if exists else {}

            if exists:
                g.user_id = user_id
                g.litres_auth_state = _auth_state(data)

                # The LitRes session expired (e.g. in a background sync) — notify once
                if EXPIRED_NOTICE_KEY in data:
                    _consume_expired_marker(user_id)
                    return

                # Check if authenticated and profile sync is stale
                email = data.get("email")
                if has_litres_tokens(data) and email:
                    # Prevent re-triggering within the same session window
                    last_trigger = session.get("_profile_sync_started")
                    should_trigger = True
                    if last_trigger:
                        try:
                            triggered_at = datetime.fromisoformat(last_trigger)
                            hours_since = (datetime.now(timezone.utc) - triggered_at).total_seconds() / 3600
                            if hours_since < app.config.get("PROFILE_SYNC_STALE_HOURS", 20):
                                should_trigger = False
                        except (ValueError, TypeError):
                            pass

                    if should_trigger:
                        _maybe_background_profile_sync(user_id, email, data)

                return

        # Create new anonymous user
        with SessionLocal() as s:
            user = User(created_at=datetime.now(timezone.utc))
            s.add(user)
            s.flush()  # get user.id
            s.add(UserSettings(user_id=user.id, genre_id=_DEFAULT_USER_GENRE_ID))
            s.commit()
            new_id = user.id

        session["user_id"] = new_id
        g.user_id = new_id
        log.debug("Created anonymous user %d", new_id)

    @app.context_processor
    def _inject_auth_notice():
        """Expose LitRes session state to all templates.

        auth_notice: one-shot Russian message (or None) after an automatic expiry.
        auth_expired: True while the account is known but its LitRes session has
            expired — show a "Войти снова" badge instead of the normal one.
        """
        return {
            "auth_notice": session.pop(_AUTH_NOTICE_SESSION_KEY, None),
            "auth_expired": g.get("litres_auth_state") == AUTH_STATE_EXPIRED,
        }
