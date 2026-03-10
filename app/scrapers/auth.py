"""LitRes authentication — login, refresh, token persistence.

Tokens are stored in User.session_data as JSON. Password is NEVER stored
in DB — it comes from env vars or CLI args.

Auth flow: stored token → refresh → re-login → persist.
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


class LitresAuthError(Exception):
    """Raised when LitRes authentication fails."""


@dataclass
class AuthResult:
    """Result from a successful login or refresh."""
    access_token: str
    refresh_token: str
    expires_in: int  # seconds


def litres_login(
    client: httpx.Client,
    email: str,
    password: str,
) -> AuthResult:
    """Authenticate with LitRes and return tokens.

    Args:
        client: An httpx.Client instance.
        email: LitRes account email.
        password: LitRes account password.

    Returns:
        AuthResult with access_token, refresh_token, and expires_in.

    Raises:
        LitresAuthError: On wrong credentials, missing token, or network error.
    """
    url = BASE_API + AUTH_LOGIN_PATH
    headers = {**DEFAULT_HEADERS, "content-type": "application/json"}

    try:
        resp = client.post(url, json={"login": email, "password": password}, headers=headers)
    except httpx.HTTPError as exc:
        raise LitresAuthError(f"Connection error during login: {exc}") from exc

    if resp.status_code == 401:
        raise LitresAuthError("Authentication failed: wrong email or password")
    if resp.status_code >= 400:
        raise LitresAuthError(f"Authentication failed: HTTP {resp.status_code}")

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
    )


def litres_refresh(
    client: httpx.Client,
    refresh_token: str,
) -> AuthResult:
    """Refresh an expired access token using the refresh token.

    Raises:
        LitresAuthError: If refresh fails (token invalid/expired, endpoint missing).
    """
    url = BASE_API + AUTH_REFRESH_PATH
    headers = {**DEFAULT_HEADERS, "content-type": "application/json"}

    try:
        resp = client.post(url, json={"refresh_token": refresh_token}, headers=headers)
    except httpx.HTTPError as exc:
        raise LitresAuthError(f"Connection error during refresh: {exc}") from exc

    if resp.status_code >= 400:
        raise LitresAuthError(
            f"Token refresh failed: HTTP {resp.status_code}"
        )

    data = resp.json()
    payload = (data.get("payload") or {}).get("data", {})
    access_token = payload.get("access_token")
    if not access_token:
        raise LitresAuthError("No access_token in refresh response")

    return AuthResult(
        access_token=access_token,
        refresh_token=payload.get("refresh_token", refresh_token),
        expires_in=payload.get("expires_in", 900),
    )


def _load_stored_tokens(session, user_id: int) -> dict | None:
    """Load stored auth data from User.session_data JSON."""
    from app.db.models import User
    user = session.get(User, user_id)
    if not user or not user.session_data:
        return None
    try:
        return json.loads(user.session_data)
    except (json.JSONDecodeError, TypeError):
        return None


def _persist_tokens(session, user_id: int, email: str, auth_result: AuthResult) -> None:
    """Save tokens to User.session_data as JSON."""
    from app.db.models import User
    user = session.get(User, user_id)
    if not user:
        return

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=auth_result.expires_in)
    user.session_data = json.dumps({
        "email": email,
        "access_token": auth_result.access_token,
        "refresh_token": auth_result.refresh_token,
        "expires_at": expires_at.isoformat(),
    })
    session.commit()
    log.info("Persisted auth tokens to User.session_data (expires_at=%s)", expires_at.isoformat())


def get_valid_token(
    *,
    session_factory,
    user_id: int,
    email: str,
    password: str | None = None,
) -> str:
    """Get a valid access token, using stored tokens if possible.

    Flow:
    1. Load stored tokens from User.session_data
    2. If access_token not expired -> return it
    3. If expired, try refresh_token -> persist new tokens -> return
    4. If refresh fails, re-login with email+password -> persist -> return
    5. If no password provided and refresh fails -> raise error

    Args:
        session_factory: SQLAlchemy sessionmaker.
        user_id: Local user ID.
        email: LitRes account email.
        password: LitRes password (from env var). None if not available.

    Returns:
        Valid JWT access_token string.

    Raises:
        LitresAuthError: If all auth methods fail.
    """
    with session_factory() as session:
        stored = _load_stored_tokens(session, user_id)

    # Check if stored token is still valid
    if stored and stored.get("access_token") and stored.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(stored["expires_at"])
            buffer = timedelta(seconds=TOKEN_EXPIRY_BUFFER_SECONDS)
            if datetime.now(timezone.utc) + buffer < expires_at:
                log.info("Using stored access token (expires %s)", stored["expires_at"])
                return stored["access_token"]
        except (ValueError, TypeError):
            pass  # Invalid date format, proceed to refresh/login

    # Try refresh
    if stored and stored.get("refresh_token"):
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                result = litres_refresh(client, stored["refresh_token"])
            log.info("Token refreshed successfully")
            with session_factory() as session:
                _persist_tokens(session, user_id, email, result)
            return result.access_token
        except LitresAuthError as exc:
            log.warning("Token refresh failed: %s — will try re-login", exc)

    # Re-login
    if not password:
        raise LitresAuthError(
            "No valid tokens and no password provided. "
            "Set LITRES_PASSWORD environment variable or pass --password."
        )

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        result = litres_login(client, email, password)

    with session_factory() as session:
        _persist_tokens(session, user_id, email, result)

    return result.access_token
