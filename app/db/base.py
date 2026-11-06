"""SQLAlchemy engine, session factory, and declarative Base.

All models import Base from here. The engine is created lazily on first use
so that importing this module never fails even before persistent/db/ exists.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """DateTime column that always reads and writes timezone-aware UTC datetimes.

    SQLAlchemy's ``DateTime(timezone=True)`` is a no-op on SQLite — the dialect
    stores datetimes as text via a format string that drops tzinfo, and reads
    them back naive. This decorator enforces UTC at the application boundary:
    aware writes are normalized to UTC, and reads always return aware UTC.

    Naive values from legacy rows are interpreted as UTC, since every writer in
    this codebase uses ``datetime.now(timezone.utc)``.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _get_database_uri() -> str:
    from app.config import Config  # noqa: PLC0415

    return Config.DATABASE_URI


def _get_database_dir() -> str:
    from app.config import Config  # noqa: PLC0415

    return Config.DATABASE_DIR


# SQLite busy_timeout (ms) applied to every new connection. CLI sync jobs keep
# the long default; the web app lowers it via set_busy_timeout() so a request
# blocked behind a sync write transaction fails fast instead of outliving
# gunicorn's worker timeout (default 30 s), see audit W1.
DEFAULT_BUSY_TIMEOUT_MS = 30000
WEB_BUSY_TIMEOUT_MS = 5000
_busy_timeout_ms = DEFAULT_BUSY_TIMEOUT_MS


def set_busy_timeout(ms: int) -> None:
    """Change busy_timeout for this process; pooled connections are recycled."""
    global _busy_timeout_ms
    if ms == _busy_timeout_ms:
        return
    _busy_timeout_ms = int(ms)
    engine.dispose()


def create_db_engine():
    """Create and return a SQLAlchemy engine, ensuring the DB directory exists."""
    os.makedirs(_get_database_dir(), exist_ok=True)
    db_engine = create_engine(
        _get_database_uri(),
        connect_args={"check_same_thread": False},
    )

    # SQLite PRAGMAs applied on every new connection:
    # - foreign_keys: enforce FK constraints (off by default in SQLite)
    # - journal_mode=WAL: allow concurrent readers + one writer without blocking
    # - busy_timeout: wait for a write lock instead of failing immediately
    #   (30 s for CLI jobs, 5 s in the web app, see set_busy_timeout)
    @event.listens_for(db_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(_busy_timeout_ms)}")
        cursor.close()

    return db_engine


# Module-level singletons — created on first import of this module.
engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
