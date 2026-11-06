"""LitRes authentication — login, refresh, token persistence, session expiry.

Tokens are stored in User.session_data as JSON. Password is NEVER stored
in DB — it comes from the login form, env vars (LITRES_EMAIL/LITRES_PASSWORD,
used only for the matching account) or CLI args.

Auth flow: stored token → refresh → re-login → persist.

Known LitRes behaviour (verified live 2026-11-06, see
docs/LitRes API Reverse Engineering.md §2.5b):
- access_token is a JWT; expires_in is now 86400 s (24 h; it was 900 s in 03/2026).
- AUTH_REFRESH_PATH returns HTTP 404 (as do other guessed refresh paths); the
  web client does not renew the JWT from the refresh cookie either. The refresh
  step is kept only so a future working endpoint is picked up for free.
- The login response's `sid` is the long-lived server session: /users/me/*
  accept `Session-Id: <sid>` instead of a Bearer JWT. This is how an expired
  JWT is bridged without a password (app/sync/profile._fetch_with_reauth).

session_data keys:
- email, access_token, refresh_token, expires_at, sid — current LitRes session
- profile_synced_at — last successful profile sync for this user (ISO, UTC)
- auth_state="expired" — set by expire_litres_session(): tokens dropped, but
  email, profile_synced_at and the cached listened books are kept.
- auth_expired_notice — one-shot flag next to auth_state="expired"; the web
  layer shows the "session expired" message once and removes the flag.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import httpx

from app.scrapers.client import BASE_API, DEFAULT_HEADERS

log = logging.getLogger(__name__)

AUTH_LOGIN_PATH = "/foundation/api/auth/login"
AUTH_REFRESH_PATH = "/foundation/api/auth/refresh"

# Consider token expired 60s before actual expiry to avoid edge cases
TOKEN_EXPIRY_BUFFER_SECONDS = 60

# Network timeouts (seconds). Refresh may run inside a page request, keep it short.
LOGIN_TIMEOUT_SECONDS = 30.0
REFRESH_TIMEOUT_SECONDS = 8.0

EXPIRED_NOTICE_KEY = "auth_expired_notice"
AUTH_STATE_KEY = "auth_state"
AUTH_STATE_EXPIRED = "expired"
PROFILE_SYNCED_AT_KEY = "profile_synced_at"


class LitresAuthError(Exception):
    """Raised when LitRes authentication fails."""


class LitresAuthRejectedError(LitresAuthError):
    """LitRes definitively rejected the credentials/tokens, or no re-auth path exists.

    The stored LitRes session is dead; the user must log in again.
    """


class LitresAuthTransientError(LitresAuthError):
    """Network error or LitRes server error — retry later, do not log the user out."""


@dataclass
class AuthResult:
    """Result from a successful login or refresh."""
    access_token: str
    refresh_token: str
    expires_in: int  # seconds
    sid: str | None = None


def litres_login(
    client: httpx.Client,
    email: str,
    password: str,
) -> AuthResult:
    """Authenticate with LitRes and return tokens.

    Raises:
        LitresAuthRejectedError: Wrong credentials (HTTP 4xx).
        LitresAuthTransientError: Network error or HTTP 5xx.
        LitresAuthError: Unexpected response shape (no access_token).
    """
    url = BASE_API + AUTH_LOGIN_PATH
    headers = {**DEFAULT_HEADERS, "content-type": "application/json"}

    try:
        resp = client.post(url, json={"login": email, "password": password}, headers=headers)
    except httpx.HTTPError as exc:
        raise LitresAuthTransientError(f"Connection error during login: {exc}") from exc

    if resp.status_code == 401:
        raise LitresAuthRejectedError("Authentication failed: wrong email or password")
    if resp.status_code >= 500:
        raise LitresAuthTransientError(f"Authentication failed: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise LitresAuthRejectedError(f"Authentication failed: HTTP {resp.status_code}")

    data = resp.json()
    payload = (data.get("payload") or {}).get("data", {})
    access_token = payload.get("access_token")
    if not access_token:
        raise LitresAuthError("No access_token in login response")

    refresh_token = payload.get("refresh_token", "")
    expires_in = payload.get("expires_in", 900)

    log.info("LitRes login successful (expires_in=%ds)", expires_in)
    return AuthResult(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        sid=payload.get("sid"),
    )


def litres_refresh(
    client: httpx.Client,
    refresh_token: str,
) -> AuthResult:
    """Refresh an expired access token using the refresh token.

    Raises:
        LitresAuthRejectedError: Refresh rejected (HTTP 4xx, incl. 404 — see module doc).
        LitresAuthTransientError: Network error or HTTP 5xx.
        LitresAuthError: Unexpected response shape.
    """
    url = BASE_API + AUTH_REFRESH_PATH
    headers = {**DEFAULT_HEADERS, "content-type": "application/json"}

    try:
        resp = client.post(url, json={"refresh_token": refresh_token}, headers=headers)
    except httpx.HTTPError as exc:
        raise LitresAuthTransientError(f"Connection error during refresh: {exc}") from exc

    if resp.status_code >= 500:
        raise LitresAuthTransientError(f"Token refresh failed: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise LitresAuthRejectedError(f"Token refresh failed: HTTP {resp.status_code}")

    data = resp.json()
    payload = (data.get("payload") or {}).get("data", {})
    access_token = payload.get("access_token")
    if not access_token:
        raise LitresAuthError("No access_token in refresh response")

    return AuthResult(
        access_token=access_token,
        refresh_token=payload.get("refresh_token", refresh_token),
        expires_in=payload.get("expires_in", 900),
        sid=payload.get("sid"),
    )


# ---------------------------------------------------------------------------
# session_data helpers
# ---------------------------------------------------------------------------


def parse_session_data(raw: str | None) -> dict:
    """Parse User.session_data JSON; returns {} for empty/invalid data."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def has_litres_tokens(data: dict) -> bool:
    """True if session_data holds a LitRes token pair (i.e. the user is logged in)."""
    return bool(data.get("access_token") and data.get("refresh_token"))


def access_token_valid(data: dict, now: datetime | None = None) -> bool:
    """True if the stored access_token exists and has not expired (with buffer)."""
    if not data.get("access_token") or not data.get("expires_at"):
        return False
    try:
        expires_at = datetime.fromisoformat(data["expires_at"])
    except (ValueError, TypeError):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now + timedelta(seconds=TOKEN_EXPIRY_BUFFER_SECONDS) < expires_at


def reauth_password_for(email: str | None) -> str | None:
    """Return a password usable for silent re-login of `email`, if one exists.

    Only the operator account configured via LITRES_EMAIL/LITRES_PASSWORD env
    vars qualifies. Passwords of other users are never stored (see report
    ESCALATION in docs/status/phase16/auth-report.md).
    """
    from app.config import Config

    if not email or not Config.LITRES_EMAIL or not Config.LITRES_PASSWORD:
        return None
    if email.strip().lower() == Config.LITRES_EMAIL.strip().lower():
        return Config.LITRES_PASSWORD
    return None


def _load_stored_tokens(session, user_id: int) -> dict | None:
    """Load stored auth data from User.session_data JSON."""
    from app.db.models import User
    user = session.get(User, user_id)
    if not user or not user.session_data:
        return None
    return parse_session_data(user.session_data) or None


def _persist_tokens(session, user_id: int, email: str, auth_result: AuthResult) -> None:
    """Save tokens to User.session_data, preserving profile_synced_at for the same account."""
    from app.db.models import User
    user = session.get(User, user_id)
    if not user:
        return

    previous = parse_session_data(user.session_data)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=auth_result.expires_in)
    data = {
        "email": email,
        "access_token": auth_result.access_token,
        "refresh_token": auth_result.refresh_token,
        "expires_at": expires_at.isoformat(),
    }
    if auth_result.sid:
        data["sid"] = auth_result.sid
    same_account = (previous.get("email") or "").lower() == (email or "").lower()
    if same_account and previous.get(PROFILE_SYNCED_AT_KEY):
        data[PROFILE_SYNCED_AT_KEY] = previous[PROFILE_SYNCED_AT_KEY]
    user.session_data = json.dumps(data)
    session.commit()
    log.info("Persisted auth tokens to User.session_data (expires_at=%s)", expires_at.isoformat())


def record_profile_synced(session_factory, user_id: int, when: datetime | None = None) -> None:
    """Store the time of the last successful profile sync in session_data."""
    from app.db.models import User

    with session_factory() as session:
        user = session.get(User, user_id)
        if not user:
            return
        data = parse_session_data(user.session_data)
        if not has_litres_tokens(data):
            return  # logged out meanwhile — do not resurrect session_data
        data[PROFILE_SYNCED_AT_KEY] = (when or datetime.now(timezone.utc)).isoformat()
        user.session_data = json.dumps(data)
        session.commit()


def expire_litres_session(session_factory, user_id: int) -> bool:
    """Mark the user's LitRes session as expired because it can no longer be renewed.

    Only the LitRes tokens are dropped. The account identity (session_data email,
    User.litres_login), profile_synced_at and the cached user_listened_book rows
    are KEPT: an expired token means "cannot refresh", not "data is wrong", so
    the listened-based filters keep working on cached data until the user logs
    in again (same email → list is refreshed; other email → replaced, see
    controllers/auth.login). A one-shot notice flag lets the next page render
    tell the user why.

    Returns True if a logged-in session was expired.
    """
    from app.db.models import User

    with session_factory() as session:
        user = session.get(User, user_id)
        if not user:
            return False
        data = parse_session_data(user.session_data)
        if not has_litres_tokens(data):
            return False
        expired = {
            "email": data.get("email") or user.litres_login or "",
            AUTH_STATE_KEY: AUTH_STATE_EXPIRED,
            EXPIRED_NOTICE_KEY: True,
            "expired_at": datetime.now(timezone.utc).isoformat(),
        }
        if data.get(PROFILE_SYNCED_AT_KEY):
            expired[PROFILE_SYNCED_AT_KEY] = data[PROFILE_SYNCED_AT_KEY]
        user.session_data = json.dumps(expired)
        session.commit()
    log.info("LitRes session expired for user %d — tokens dropped, listened data kept", user_id)
    return True


def is_litres_session_expired(data: dict) -> bool:
    """True if session_data describes an expired (not logged-out) LitRes session."""
    return data.get(AUTH_STATE_KEY) == AUTH_STATE_EXPIRED and not has_litres_tokens(data)


def get_valid_token(
    *,
    session_factory,
    user_id: int,
    email: str,
    password: str | None = None,
    force_renew: bool = False,
    force_login: bool = False,
) -> str:
    """Get a valid access token, using stored tokens if possible.

    Flow:
    1. Load stored tokens from User.session_data
    2. If access_token not expired (and not force_renew/force_login) -> return it
    3. Try refresh_token (skipped with force_login) -> persist -> return
    4. Re-login with password (explicit, else env creds for this email) -> persist -> return

    Args:
        session_factory: SQLAlchemy sessionmaker.
        user_id: Local user ID.
        email: LitRes account email.
        password: LitRes password. None → env creds if they match `email`.
        force_renew: Ignore a not-yet-expired stored access token (e.g. LitRes
            answered 401 with it).
        force_login: Skip stored tokens and refresh entirely (explicit login form).

    Raises:
        LitresAuthRejectedError: Session cannot be renewed (no password, or rejected).
        LitresAuthTransientError: Renewal failed for a transient reason.
        LitresAuthError: Unexpected response shape.
    """
    if password is None:
        password = reauth_password_for(email)

    stored = None
    if not force_login:
        with session_factory() as session:
            stored = _load_stored_tokens(session, user_id)

    if stored and not force_renew and access_token_valid(stored):
        log.info("Using stored access token (expires %s)", stored["expires_at"])
        return stored["access_token"]

    transient: LitresAuthTransientError | None = None

    # Try refresh
    if stored and stored.get("refresh_token"):
        try:
            with httpx.Client(timeout=REFRESH_TIMEOUT_SECONDS, follow_redirects=True) as client:
                result = litres_refresh(client, stored["refresh_token"])
            log.info("Token refreshed successfully")
            with session_factory() as session:
                _persist_tokens(session, user_id, email, result)
            return result.access_token
        except LitresAuthTransientError as exc:
            transient = exc
            log.warning("Token refresh failed (transient): %s", exc)
        except LitresAuthError as exc:
            log.warning("Token refresh failed: %s — will try re-login", exc)

    # Re-login
    if not password:
        if transient is not None:
            raise LitresAuthTransientError(f"Cannot renew LitRes session right now: {transient}") from transient
        raise LitresAuthRejectedError(
            "No valid tokens and no password provided. "
            "Set LITRES_PASSWORD environment variable or pass --password."
        )

    with httpx.Client(timeout=LOGIN_TIMEOUT_SECONDS, follow_redirects=True) as client:
        result = litres_login(client, email, password)

    with session_factory() as session:
        _persist_tokens(session, user_id, email, result)

    return result.access_token
