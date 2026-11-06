"""
Application configuration (centralized config, similar in spirit to Spring Boot).
Flask does not auto-load a single config file; loading is done in app via
app.config.from_object(Config) or from_pyfile().
"""

import os

# Project root (directory containing app/, templates/, static/)
# app/config/config.py -> app/config -> app -> project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(_THIS_DIR)
BASE_DIR = os.path.dirname(_APP_DIR)


class Config:
    """
    Configuration object for app.config.from_object(Config).
    Template and static folders are set here and passed to Flask(...) at app creation,
    since template_folder and static_folder are not read from app.config automatically.
    """

    # Paths to templates and static files (relative to project root, Phase 2 layout)
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
    STATIC_FOLDER = os.path.join(BASE_DIR, "static")

    # Session secret (set via env in production). The fallback is for local dev
    # and tests only: create_app refuses to serve it under gunicorn (audit W10).
    INSECURE_DEV_SECRET_KEY = "dev-secret-change-in-production"
    SECRET_KEY = os.environ.get("SECRET_KEY") or INSECURE_DEV_SECRET_KEY

    # Persistent session: keep users logged in for 365 days
    from datetime import timedelta
    PERMANENT_SESSION_LIFETIME = timedelta(days=365)

    # Session cookie hardening (audit W2/W8). SameSite=Lax stops cross-site
    # POSTs from carrying the cookie; HttpOnly is Flask's default.
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    # Secure flag: "auto" (default) sets it when the request arrived over HTTPS
    # (directly or via X-Forwarded-Proto from the TLS proxy), so plain-http
    # local dev keeps working; "1"/"0" force it on/off.
    SESSION_COOKIE_SECURE_MODE: str = os.environ.get("SESSION_COOKIE_SECURE", "auto").strip().lower()

    # Extra origins allowed to send state-changing requests (CSRF Origin check),
    # comma-separated hosts or origins, e.g. "litres.example.org". The request's
    # own Host / X-Forwarded-Host is always allowed.
    TRUSTED_ORIGINS: set[str] = {
        o.strip().lower() for o in os.environ.get("TRUSTED_ORIGINS", "").split(",") if o.strip()
    }

    # Number of reverse proxies in front of the app whose X-Forwarded-For entry
    # is trusted for the client IP (login rate limit). 0 = use the socket peer.
    TRUSTED_PROXY_HOPS: int = int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or 0)

    # /auth/login throttle (audit W7): attempts per window, per client IP and per email.
    LOGIN_RATE_WINDOW_SECONDS: int = 600
    LOGIN_RATE_MAX_PER_IP: int = 20
    LOGIN_RATE_MAX_PER_EMAIL: int = 5

    # Profile sync staleness threshold (hours) — auto-resync if older
    PROFILE_SYNC_STALE_HOURS: int = 20
    # A started (or failed) background profile sync blocks a new one for this
    # long — the server-side, cross-worker throttle (audit W6/W12).
    PROFILE_SYNC_RETRY_MINUTES: int = 15

    # Debug mode
    DEBUG = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")

    # Database (Phase 5+): SQLite in persistent/db/
    DATABASE_DIR = os.path.join(BASE_DIR, "persistent", "db")
    DATABASE_URI = os.environ.get("DATABASE_URI") or (
        f"sqlite:///{os.path.join(BASE_DIR, 'persistent', 'db', 'litres_cache.db')}"
    )

    # Cover images cache
    COVERS_DIR = os.path.join(BASE_DIR, "persistent", "covers")

    # External URLs (for links to LitRes)
    LITRES_BASE = "https://www.litres.ru"

    # Default genre shown on first load (Легкое чтение — root of synced scope)
    DEFAULT_GENRE_ID: str = "201583"

    # LitRes scraper rate limiting (~2 req/s with small jitter; empirically acceptable)
    # 0.5s minimum delay = 2 req/s max. Jitter spreads bursts without slowing average.
    SCRAPER_MIN_DELAY_SECONDS: float = 0.5
    SCRAPER_JITTER_SECONDS: float = 0.2

    # Sync output directory (logs, failed_books.jsonl)
    SYNC_DIR = os.path.join(BASE_DIR, "persistent", "sync")

    # Admin panel: comma-separated list of LitRes emails with admin access
    _admin_emails_raw = os.environ.get("ADMIN_EMAILS", "")
    ADMIN_EMAILS: set[str] | None = (
        {e.strip().lower() for e in _admin_emails_raw.split(",") if e.strip()}
        if _admin_emails_raw else None
    )

    # LitRes credentials for profile sync (v1.1)
    LITRES_EMAIL: str | None = os.environ.get("LITRES_EMAIL")
    LITRES_PASSWORD: str | None = os.environ.get("LITRES_PASSWORD")

    # Accessible as a class-level attr for use outside Flask context
    BASE_DIR = BASE_DIR
