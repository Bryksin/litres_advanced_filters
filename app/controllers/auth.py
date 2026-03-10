"""Auth controller — login/logout/status endpoints for LitRes auth."""

import json
import logging

from flask import Blueprint, g, jsonify, request

from app.db import SessionLocal
from app.db.models import User, UserListenedBook
from app.scrapers.auth import get_valid_token, LitresAuthError
from app.sync.profile import run_profile

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _get_listened_count(session_factory, user_id: int) -> int:
    """Count listened books for a user."""
    with session_factory() as s:
        return s.query(UserListenedBook).filter(
            UserListenedBook.user_id == user_id,
        ).count()


def _is_authenticated(session_factory, user_id: int) -> tuple[bool, str | None]:
    """Check if user has valid stored tokens. Returns (is_auth, email)."""
    with session_factory() as s:
        user = s.get(User, user_id)
        if not user or not user.session_data:
            return False, None
        try:
            data = json.loads(user.session_data)
        except (json.JSONDecodeError, TypeError):
            return False, None

        if data.get("access_token") and data.get("refresh_token"):
            return True, data.get("email")
        return False, None


@bp.route("/login", methods=["POST"])
def login():
    """Login to LitRes and sync listened books."""
    body = request.get_json(silent=True) or {}
    email = body.get("email", "").strip()
    password = body.get("password", "").strip()

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password are required"}), 400

    # Step 1: Authenticate (fail hard if this doesn't work)
    try:
        get_valid_token(
            session_factory=SessionLocal,
            user_id=g.user_id,
            email=email,
            password=password,
        )
    except LitresAuthError as exc:
        log.warning("Login failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception:
        log.exception("Login error")
        return jsonify({"ok": False, "error": "Internal error"}), 500

    # Persist litres_login on the User row
    with SessionLocal() as s:
        user = s.get(User, g.user_id)
        if user:
            user.litres_login = email
            s.commit()

    # Step 2: Sync profile (best-effort — auth already succeeded)
    sync_error = None
    try:
        run_profile(
            session_factory=SessionLocal,
            email=email,
            password=password,
            user_id=g.user_id,
        )
    except Exception:
        log.exception("Profile sync failed after successful login")
        sync_error = "Login successful, but listened books sync failed"

    count = _get_listened_count(SessionLocal, g.user_id)
    log.info("Login complete: %d listened books (sync_error=%s)", count, sync_error)

    result = {"ok": True, "listened_count": count, "email": email}
    if sync_error:
        result["warning"] = sync_error
    return jsonify(result)


@bp.route("/logout", methods=["POST"])
def logout():
    """Clear stored auth tokens and listened books."""
    with SessionLocal() as s:
        user = s.get(User, g.user_id)
        if user:
            user.session_data = None
            user.litres_login = None
        # Listened books belong to LitRes account, not anonymous session
        s.query(UserListenedBook).filter(
            UserListenedBook.user_id == g.user_id,
        ).delete(synchronize_session="fetch")
        s.commit()
    return jsonify({"ok": True})


@bp.route("/status", methods=["GET"])
def status():
    """Check current auth status without making HTTP calls."""
    is_auth, email = _is_authenticated(SessionLocal, g.user_id)
    result = {"authenticated": is_auth, "email": email}
    if is_auth:
        result["listened_count"] = _get_listened_count(SessionLocal, g.user_id)
    return jsonify(result)
