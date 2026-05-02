"""Session middleware — ensures every request has a valid g.user_id.

Also handles persistent sessions and background profile sync.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, g, session

from app.db import SessionLocal
from app.db.models import User, UserSettings

# Default genre for new anonymous users (Космическая фантастика).
# Using root genre (201583) would query all 91k books — too slow for first load.
_DEFAULT_USER_GENRE_ID = "5077"

log = logging.getLogger(__name__)

# Paths that should not trigger user creation or profile sync
_SKIP_PREFIXES = ("/static/", "/covers/")


def _is_authenticated(user: User) -> tuple[bool, str | None, str | None]:
    """Check if user has valid stored tokens. Returns (is_auth, email, password)."""
    if not user or not user.session_data:
        return False, None, None
    try:
        data = json.loads(user.session_data)
    except (json.JSONDecodeError, TypeError):
        return False, None, None

    if data.get("access_token") and data.get("refresh_token"):
        return True, data.get("email"), None
    return False, None, None


def _maybe_background_profile_sync(user_id: int, email: str) -> None:
    """Trigger background profile sync if the last one is stale (>PROFILE_SYNC_STALE_HOURS)."""
    from app.config import Config
    from app.db.models import SyncRun

    stale_hours = Config.PROFILE_SYNC_STALE_HOURS

    with SessionLocal() as s:
        last_profile_run = (
            s.query(SyncRun)
            .filter(SyncRun.type == "profile")
            .order_by(SyncRun.started_at.desc())
            .first()
        )

        if last_profile_run and last_profile_run.started_at:
            age_hours = (datetime.now(timezone.utc) - last_profile_run.started_at).total_seconds() / 3600
            if age_hours < stale_hours:
                return  # Still fresh

    log.info("Profile sync stale (>%dh) — triggering background sync for user %d", stale_hours, user_id)
    session["_profile_sync_started"] = datetime.now(timezone.utc).isoformat()

    def _run():
        try:
            from app.sync.profile import run_profile
            run_profile(
                session_factory=SessionLocal,
                email=email,
                user_id=user_id,
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
            # Validate user still exists
            with SessionLocal() as s:
                user = s.get(User, user_id)
                if user is not None:
                    g.user_id = user_id

                    # Check if authenticated and profile sync is stale
                    is_auth, email, _ = _is_authenticated(user)
                    if is_auth and email:
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
                            _maybe_background_profile_sync(user_id, email)

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
