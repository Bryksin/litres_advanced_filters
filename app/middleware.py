"""Session middleware — ensures every request has a valid g.user_id."""

import logging
from datetime import datetime, timezone

from flask import Flask, g, session

from app.db import SessionLocal
from app.db.models import User, UserSettings

# Default genre for new anonymous users (Космическая фантастика).
# Using root genre (201583) would query all 91k books — too slow for first load.
_DEFAULT_USER_GENRE_ID = "5077"

log = logging.getLogger(__name__)

# Paths that should not trigger user creation
_SKIP_PREFIXES = ("/static/", "/covers/")


def register_session_middleware(app: Flask) -> None:
    """Register before_request handler that resolves session -> g.user_id."""

    @app.before_request
    def _ensure_user():
        from flask import request

        # Skip for static assets
        if any(request.path.startswith(p) for p in _SKIP_PREFIXES):
            return

        user_id = session.get("user_id")

        if user_id is not None:
            # Validate user still exists
            with SessionLocal() as s:
                user = s.get(User, user_id)
                if user is not None:
                    g.user_id = user_id
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
