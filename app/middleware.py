"""Session and security middleware.

Resolves the session cookie to `g.user_id`. A visitor without a cookie (or
with a stale one) is anonymous: `g.user_id == ANONYMOUS_USER_ID`, which never
matches a row, so reads see default settings and empty lists. The `user` and
`user_settings` rows are created lazily, only when the request needs state
(a state-changing request or a filter form submit), see ensure_user(). Plain
page views, HEAD requests, 404s and bots therefore never write (audit W1/W9).

Also handles persistent sessions, the server-side throttled background
profile sync, LitRes session expiry (`auth_notice` one-shot / `auth_expired`
for templates), the CSRF Origin check, cookie flags and security headers.
"""

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, session
from flask.sessions import SecureCookieSessionInterface

from app.db import SessionLocal
from app.db.models import User, UserSettings
from app.scrapers.auth import (
    AUTH_STATE_EXPIRED,
    EXPIRED_NOTICE_KEY,
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

from app.sync.profile import (
    claim_profile_sync,
    profile_sync_claim_active,
    profile_sync_is_stale,
)

log = logging.getLogger(__name__)

# g.user_id of a visitor without a user row (ids start at 1, so 0 matches nothing)
ANONYMOUS_USER_ID = 0

# Paths that should not trigger user resolution or profile sync
_SKIP_PREFIXES = ("/static/", "/covers/")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Endpoints whose non-GET request needs no user row
# (auth.login creates it itself, only after input validation and rate limiting)
_NO_USER_ROW_ENDPOINTS = frozenset({"auth.logout", "auth.login"})


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
    return profile_sync_is_stale(data, stale_hours)


def _set_expired_notice() -> None:
    """Flag the current browser session to show the 'LitRes session expired' message."""
    session[_AUTH_NOTICE_SESSION_KEY] = AUTH_EXPIRED_MESSAGE
    session.pop("is_admin", None)
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
    """Start a background profile sync if this user's last one is stale.

    The throttle is server-side (audit W6): a claim timestamp in the user's
    session_data, taken with a compare-and-swap, so parallel requests, other
    gunicorn workers or a replayed cookie cannot start a second sync within
    PROFILE_SYNC_RETRY_MINUTES. The claim is taken *before* the (possibly
    synchronous) renewability check, so a LitRes outage costs one slow
    request per retry window, not one per page view (audit W12).
    """
    from flask import current_app

    stale_hours = current_app.config.get("PROFILE_SYNC_STALE_HOURS", 20)
    retry_minutes = current_app.config.get("PROFILE_SYNC_RETRY_MINUTES", 15)
    if data is None:
        with SessionLocal() as s:
            user = s.get(User, user_id)
            data = parse_session_data(user.session_data) if user else {}

    # Cheap, write-free pre-checks on the data already loaded for this request
    if not _profile_sync_is_stale(data, stale_hours):
        return
    if profile_sync_claim_active(data, retry_minutes):
        return

    if not claim_profile_sync(
        SessionLocal, user_id, retry_minutes=retry_minutes, stale_hours=stale_hours,
    ):
        return  # another request/worker got it, or the DB is busy

    if not _litres_session_renewable(user_id, email, data):
        return

    log.info("Profile sync stale (>%dh) — triggering background sync for user %d", stale_hours, user_id)
    start_profile_sync_thread(user_id=user_id, email=email, expire_on_auth_failure=True)


def start_profile_sync_thread(
    *, user_id: int, email: str, password: str | None = None, expire_on_auth_failure: bool = False,
) -> threading.Thread:
    """Run app.sync.profile.run_profile in a daemon thread (caller holds the claim)."""

    def _run():
        try:
            from app.sync.profile import run_profile
            run_profile(
                session_factory=SessionLocal,
                email=email,
                password=password,
                user_id=user_id,
                expire_on_auth_failure=expire_on_auth_failure,
            )
            log.info("Background profile sync completed for user %d", user_id)
        except Exception:
            log.exception("Background profile sync failed for user %d", user_id)

    t = threading.Thread(target=_run, daemon=True, name=f"profile-sync-{user_id}")
    t.start()
    return t


# ---------------------------------------------------------------------------
# Admin check (shared by the admin blueprint and the per-request UI flag)
# ---------------------------------------------------------------------------


def admin_email_for(user: User | None, admin_emails: set[str] | None = None) -> str | None:
    """Email of `user` if it may use the admin panel, else None.

    Requires ADMIN_EMAILS to be configured, an ACTIVE LitRes session (live
    tokens, not expired or logged out) and that session's email in the
    allowlist (audit W5). The email is taken from the session's own data, so
    a stale `litres_login` alone grants nothing. `admin_emails` defaults to
    Config.ADMIN_EMAILS.
    """
    if admin_emails is None:
        from app.config import Config

        admin_emails = Config.ADMIN_EMAILS
    if not admin_emails or user is None:
        return None
    data = parse_session_data(user.session_data)
    if not has_litres_tokens(data):
        return None
    email = (data.get("email") or "").strip().lower()
    if not email or email not in admin_emails:
        return None
    return email


def _sync_admin_flag(user: User) -> None:
    """Keep the UI-only session['is_admin'] flag in step with the real check (W17)."""
    is_admin = admin_email_for(user) is not None
    if is_admin and not session.get("is_admin"):
        session["is_admin"] = True
    elif not is_admin and "is_admin" in session:
        session.pop("is_admin", None)


# ---------------------------------------------------------------------------
# Lazy user creation
# ---------------------------------------------------------------------------


def _create_user() -> int:
    from app.config import Config

    with SessionLocal() as s:
        user = User(created_at=datetime.now(timezone.utc))
        s.add(user)
        s.flush()  # get user.id
        s.add(UserSettings(user_id=user.id, genre_id=Config.DEFAULT_GENRE_ID))
        s.commit()
        return user.id


def ensure_user() -> int:
    """Return the current user's id, creating the user row first if anonymous.

    Call from any code path that is about to store per-user state.
    """
    if g.get("user_id") not in (None, ANONYMOUS_USER_ID):
        return g.user_id
    new_id = _create_user()
    session.permanent = True
    session["user_id"] = new_id
    g.user_id = new_id
    log.debug("Created user %d", new_id)
    return new_id


def _request_needs_user_row() -> bool:
    """Does this request store per-user state (so an anonymous visitor needs a row)?"""
    if request.url_rule is None:
        return False  # 404/405 — nothing will be stored
    if request.method not in _SAFE_METHODS:
        return request.endpoint not in _NO_USER_ROW_ENDPOINTS
    # The catalog filter form is a GET that saves the submitted settings, and
    # a sort change is remembered too (ruling Q-S6)
    return request.endpoint == "catalog.index" and (
        "_submit" in request.args or bool(request.args.get("sort"))
    )


# ---------------------------------------------------------------------------
# CSRF (Origin/Referer) check, cookie flags, security headers
# ---------------------------------------------------------------------------


def _hostname(value: str) -> str:
    """Lower-cased hostname of an origin/URL/host[:port] string ('' if none)."""
    value = value.strip().lower()
    if "://" not in value:
        value = "//" + value
    try:
        return urlsplit(value).hostname or ""
    except ValueError:
        return ""


def _trusted_hostnames() -> set[str]:
    from app.config import Config

    hosts = {_hostname(request.host)}
    fwd_host = request.headers.get("X-Forwarded-Host", "")
    if fwd_host:
        hosts.add(_hostname(fwd_host.split(",")[0]))
    hosts.update(_hostname(o) for o in Config.TRUSTED_ORIGINS)
    hosts.discard("")
    return hosts


def request_origin_allowed() -> bool:
    """Same-origin check for a state-changing request (audit W2).

    Browsers send `Origin` on every cross-origin POST (form or fetch) and on
    same-origin fetch POSTs; `Referer` is the fallback. A request carrying
    neither is not a browser cross-site request (CLI/tests) and is allowed;
    `Origin: null` (sandboxed frame, opaque redirect) is rejected.
    """
    origin = request.headers.get("Origin")
    source = origin if origin is not None else request.headers.get("Referer")
    if source is None or source == "":
        return origin is None
    return _hostname(source) in _trusted_hostnames()


class _AutoSecureSessionInterface(SecureCookieSessionInterface):
    """Signed-cookie sessions whose Secure flag can follow the request scheme."""

    def get_cookie_secure(self, app: Flask) -> bool:
        mode = str(app.config.get("SESSION_COOKIE_SECURE_MODE", "auto")).lower()
        if mode in ("1", "true", "yes", "on"):
            return True
        if mode in ("0", "false", "no", "off"):
            return False
        if request.is_secure:
            return True
        proto = request.headers.get("X-Forwarded-Proto", "")
        return proto.split(",")[0].strip().lower() == "https"


# The templates use inline <script> blocks and on* handler attributes, so
# script-src needs 'unsafe-inline' (nonces would not cover the handlers).
# The CSP still blocks external scripts, plugins, <base> hijacking, foreign
# form targets and framing.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _enforce_secret_key(app: Flask) -> None:
    """Never serve production traffic with the public fallback SECRET_KEY (W10).

    The session cookie holds only the user id, so a known key lets anyone
    forge any user (admin included). Under gunicorn (the production server)
    startup fails; the dev server and tests only get a warning.
    """
    from app.config import Config

    if app.config.get("SECRET_KEY") != Config.INSECURE_DEV_SECRET_KEY:
        return
    if "gunicorn" in sys.modules and not app.debug:
        raise RuntimeError(
            "SECRET_KEY is not set. Refusing to start: the built-in development "
            "key would let anyone forge session cookies. Set the SECRET_KEY env var."
        )
    # Dev server / tests: app.start already logs a warning


def register_session_middleware(app: Flask) -> None:
    """Register session resolution, CSRF check, cookie flags and security headers."""
    from app.db.base import WEB_BUSY_TIMEOUT_MS, set_busy_timeout

    _enforce_secret_key(app)
    # Web requests must fail fast on a locked DB, well inside gunicorn's timeout
    set_busy_timeout(WEB_BUSY_TIMEOUT_MS)
    app.session_interface = _AutoSecureSessionInterface()

    @app.before_request
    def _csrf_guard():
        if request.method in _SAFE_METHODS:
            return None
        if request_origin_allowed():
            return None
        log.warning(
            "Rejected cross-origin %s %s (Origin=%r Referer=%r Host=%r)",
            request.method, request.path, request.headers.get("Origin"),
            request.headers.get("Referer"), request.host,
        )
        return jsonify({"ok": False, "error": "Запрос отклонён: недопустимый источник"}), 403

    @app.before_request
    def _ensure_user():
        g.user_id = ANONYMOUS_USER_ID

        # Skip for static assets
        if any(request.path.startswith(p) for p in _SKIP_PREFIXES):
            return

        user_id = session.get("user_id")

        if user_id is not None:
            # Validate user still exists (read what we need, release the DB session)
            with SessionLocal() as s:
                user = s.get(User, user_id)
                exists = user is not None
                data = parse_session_data(user.session_data) if exists else {}
                if exists:
                    _sync_admin_flag(user)

            if exists:
                # Make all sessions persistent (cookie survives browser close)
                session.permanent = True
                g.user_id = user_id
                g.litres_auth_state = _auth_state(data)

                # Drop the obsolete cookie-side sync throttle (now server-side)
                session.pop("_profile_sync_started", None)

                # The LitRes session expired (e.g. in a background sync) — notify once
                if EXPIRED_NOTICE_KEY in data:
                    _consume_expired_marker(user_id)
                    return

                email = data.get("email")
                if has_litres_tokens(data) and email:
                    _maybe_background_profile_sync(user_id, email, data)
                return

            # Stale cookie (user row gone): continue as a fresh anonymous visitor
            session.pop("user_id", None)
            session.pop("is_admin", None)

        session.pop("is_admin", None)
        if _request_needs_user_row():
            ensure_user()

    @app.after_request
    def _security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

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
