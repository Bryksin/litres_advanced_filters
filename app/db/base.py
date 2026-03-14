"""SQLAlchemy engine, session factory, and declarative Base.

All models import Base from here. The engine is created lazily on first use
so that importing this module never fails even before persistent/db/ exists.
"""

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def _get_database_uri() -> str:
    from app.config import Config  # noqa: PLC0415

    return Config.DATABASE_URI


def _get_database_dir() -> str:
    from app.config import Config  # noqa: PLC0415

    return Config.DATABASE_DIR


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
    # - busy_timeout: wait up to 30s for a write lock instead of failing immediately
    @event.listens_for(db_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return db_engine


# Module-level singletons — created on first import of this module.
engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
