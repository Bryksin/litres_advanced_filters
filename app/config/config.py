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

    # Session secret (set via env in production)
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-change-in-production"

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

    # LitRes credentials for profile sync (v1.1)
    LITRES_EMAIL: str | None = os.environ.get("LITRES_EMAIL")
    LITRES_PASSWORD: str | None = os.environ.get("LITRES_PASSWORD")

    # Accessible as a class-level attr for use outside Flask context
    BASE_DIR = BASE_DIR
