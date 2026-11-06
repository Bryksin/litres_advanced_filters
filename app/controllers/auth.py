"""Auth controller — login/logout/status endpoints for LitRes auth."""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify, request, session

from app.config import Config
from app.db import SessionLocal
from app.db.models import User, UserListenedBook
from app.scrapers.auth import (
    LitresAuthError,
    LitresAuthRejectedError,
    LitresAuthTransientError,
    get_valid_token,
    has_litres_tokens,
    is_litres_session_expired,
    parse_session_data,
)
from app.sync.profile import claim_profile_sync, run_profile

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _get_listened_count(session_factory, user_id: int) -> int:
    """Count listened books for a user."""
    with session_factory() as s:
        return s.query(UserListenedBook).filter(
            UserListenedBook.user_id == user_id,
        ).count()


def _auth_data(session_factory, user_id: int) -> dict:
    with session_factory() as s:
        user = s.get(User, user_id)
        return parse_session_data(user.session_data) if user else {}


def _is_authenticated(session_factory, user_id: int) -> tuple[bool, str | None]:
    """Check if a LitRes account is linked to this user. Returns (is_auth, email).

    True both for a live session and for an *expired* one (tokens dropped by the
    middleware because they could not be renewed). An expired account keeps its
    cached listened books, so the listened-based filters stay usable; the
    template distinguishes the two via `auth_expired` (middleware context
    processor) and /auth/status reports `expired`. Network-free.
    """
    data = _auth_data(session_factory, user_id)
    if has_litres_tokens(data) or is_litres_session_expired(data):
        return True, data.get("email")
    return False, None


def _previous_account_email(session_factory, user_id: int) -> str | None:
    """Email of the LitRes account currently linked (live or expired), if any."""
    with session_factory() as s:
        user = s.get(User, user_id)
        if not user:
            return None
        data = parse_session_data(user.session_data)
        return data.get("email") or user.litres_login


class _SlidingWindowLimiter:
    """In-process sliding-window counter: at most `limit` hits per key per window.

    Per gunicorn worker (4 in production), so the effective cap is up to 4x;
    that is enough to stop a script from using /auth/login as a credential
    oracle against LitRes from our IP (audit W7) without a new dependency.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float, now: float | None = None) -> bool:
        """Record a hit; False (and not recorded) if the key is over its limit."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 10000:  # bound memory under a key-spraying attack
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= window]:
                    del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def login_limiter() -> _SlidingWindowLimiter:
    """The current app's limiter (one per app instance, i.e. per worker)."""
    return current_app.extensions.setdefault("login_limiter", _SlidingWindowLimiter())

# Fixed Russian copy for every auth error response. Exception text never
# reaches the client; the details are logged server-side (F9).
LOGIN_RATE_LIMITED_MESSAGE = "Слишком много попыток входа. Попробуйте ещё раз через несколько минут."
LOGIN_EMPTY_FIELDS_MESSAGE = "Введите email и пароль"
LOGIN_WRONG_CREDENTIALS_MESSAGE = "Неверный email или пароль"
LOGIN_REFUSED_MESSAGE = "ЛитРес отклонил вход. Попробуйте позже"
LOGIN_UNAVAILABLE_MESSAGE = "ЛитРес недоступен, попробуйте позже"
LOGIN_FAILED_MESSAGE = "Не удалось войти. Попробуйте позже"
REQUEST_FAILED_MESSAGE = "Не удалось выполнить запрос. Попробуйте позже"


def _error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _login_error_response(exc: LitresAuthError):
    """Map a LitRes auth failure to (fixed Russian message, HTTP status)."""
    if isinstance(exc, LitresAuthTransientError):
        return _error(LOGIN_UNAVAILABLE_MESSAGE, 503)
    if isinstance(exc, LitresAuthRejectedError):
        if exc.status_code in (None, 400, 401, 403, 404, 422):
            return _error(LOGIN_WRONG_CREDENTIALS_MESSAGE, 401)
        # e.g. 429 from LitRes: not the user's password
        return _error(LOGIN_REFUSED_MESSAGE, 503)
    return _error(LOGIN_FAILED_MESSAGE, 502)  # unexpected LitRes response shape


@bp.errorhandler(Exception)
def _json_error(exc):
    """Any other error on /auth/*: JSON with a fixed Russian message, details in the log."""
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return _error(REQUEST_FAILED_MESSAGE, exc.code or 500)
    log.exception("Unhandled error on %s", request.path)
    return _error(REQUEST_FAILED_MESSAGE, 500)


def _client_ip() -> str:
    """Client IP; trusts the last TRUSTED_PROXY_HOPS X-Forwarded-For entries."""
    hops = int(current_app.config.get("TRUSTED_PROXY_HOPS", 0))
    if hops > 0:
        forwarded = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.remote_addr or "unknown"


def _login_allowed(email: str) -> bool:
    cfg = current_app.config
    window = cfg.get("LOGIN_RATE_WINDOW_SECONDS", 600)
    limiter = login_limiter()
    ip_ok = limiter.hit(f"ip:{_client_ip()}", cfg.get("LOGIN_RATE_MAX_PER_IP", 20), window)
    email_ok = ip_ok and limiter.hit(
        f"email:{email.lower()}", cfg.get("LOGIN_RATE_MAX_PER_EMAIL", 5), window,
    )
    return ip_ok and email_ok


@bp.route("/login", methods=["POST"])
def login():
    """Login to LitRes and sync listened books."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    email = body.get("email")
    password = body.get("password")
    email = email.strip() if isinstance(email, str) else ""
    # Passwords are sent as typed: leading/trailing spaces may be part of them
    password = password if isinstance(password, str) else ""

    if not email or not password:
        return _error(LOGIN_EMPTY_FIELDS_MESSAGE, 400)

    if not _login_allowed(email):
        log.warning("Login rate limit hit (ip=%s)", _client_ip())
        return _error(LOGIN_RATE_LIMITED_MESSAGE, 429)

    # The tokens are stored on the user row: create it now if still anonymous
    from app.middleware import ensure_user
    ensure_user()

    previous_email = _previous_account_email(SessionLocal, g.user_id)

    # Step 1: Authenticate (fail hard if this doesn't work)
    try:
        get_valid_token(
            session_factory=SessionLocal,
            user_id=g.user_id,
            email=email,
            password=password,
            # Always verify the submitted credentials; never reuse a stored
            # token (it may belong to a different LitRes account).
            force_login=True,
        )
    except LitresAuthError as exc:
        log.warning("Login failed (%s): %s", type(exc).__name__, exc)
        return _login_error_response(exc)
    except Exception:
        log.exception("Login error")
        return _error(LOGIN_FAILED_MESSAGE, 500)

    # Persist litres_login on the User row. Listened books belong to the LitRes
    # account: same account (e.g. re-login after expiry) keeps the cached list
    # and the sync below refreshes it; a different account replaces it.
    switched_account = bool(previous_email) and previous_email.strip().lower() != email.lower()
    with SessionLocal() as s:
        user = s.get(User, g.user_id)
        if user:
            user.litres_login = email
        if switched_account:
            s.query(UserListenedBook).filter(
                UserListenedBook.user_id == g.user_id,
            ).delete(synchronize_session="fetch")
        s.commit()

    # UI flag for the admin link; the real gate is admin_panel._require_admin,
    # and the middleware re-derives this flag on every request.
    if Config.ADMIN_EMAILS and email.strip().lower() in Config.ADMIN_EMAILS:
        session["is_admin"] = True
    else:
        session.pop("is_admin", None)

    # Step 2: Trigger background profile sync (non-blocking). The DB claim
    # prevents a duplicate when a sync for this account is already running.
    user_id = g.user_id
    syncing = claim_profile_sync(
        SessionLocal, user_id,
        retry_minutes=current_app.config.get("PROFILE_SYNC_RETRY_MINUTES", 15),
        require_tokens=False,
    )
    if syncing:
        def _bg_sync():
            try:
                run_profile(
                    session_factory=SessionLocal,
                    email=email,
                    password=password,
                    user_id=user_id,
                )
                log.info("Background profile sync completed after login for user %d", user_id)
            except Exception:
                log.exception("Background profile sync failed after login for user %d", user_id)

        threading.Thread(target=_bg_sync, daemon=True, name=f"login-profile-sync-{user_id}").start()
    else:
        log.info("Profile sync for user %d already running — not starting another", user_id)

    # Informational only (the throttle is server-side now)
    session["_profile_sync_started"] = datetime.now(timezone.utc).isoformat()

    count = _get_listened_count(SessionLocal, g.user_id)
    log.info("Login complete: %d listened books (profile sync running in background)", count)

    return jsonify({
        "ok": True,
        "listened_count": count,
        "email": email,
        "syncing": syncing,
    })


@bp.route("/logout", methods=["POST"])
def logout():
    """Clear stored auth tokens and listened books."""
    session.pop("is_admin", None)
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
    result = {
        "authenticated": is_auth,
        "email": email,
        "expired": is_litres_session_expired(_auth_data(SessionLocal, g.user_id)),
    }
    if is_auth:
        result["listened_count"] = _get_listened_count(SessionLocal, g.user_id)
    return jsonify(result)
