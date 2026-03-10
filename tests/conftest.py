"""Pytest configuration and shared fixtures.

DATABASE_URI must be set before any app module is imported so Config picks up
the test DB path at class-definition time.

app/__init__.py has no module-level side effects (no create_app() call), so
importing any app.* submodule during test collection is safe.

Transaction isolation strategy: each test uses a dedicated in-memory SQLite DB
(fresh engine per test). This is the simplest reliable approach for SQLite —
no SAVEPOINT complications, and tests are fully isolated from each other.
"""
import os
import subprocess
import sys

# Compute absolute path to test DB, relative to this file's directory.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEST_DB_PATH = os.path.join(_TESTS_DIR, "litres_test.db")
TEST_DB_URL = f"sqlite:///{_TEST_DB_PATH}"

# Override DB path BEFORE any app module is imported.
os.environ["DATABASE_URI"] = TEST_DB_URL

# Remove stale test DB, then run fresh migrations in a subprocess.
if os.path.exists(_TEST_DB_PATH):
    os.remove(_TEST_DB_PATH)

os.makedirs(_TESTS_DIR, exist_ok=True)

_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)

result = subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "head"],
    cwd=_PROJECT_ROOT,
    env={**os.environ, "DATABASE_URI": TEST_DB_URL},
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    raise RuntimeError(
        f"Alembic migration failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

import logging  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Import Base after migrations ran so models are registered.
from app.db.base import Base  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_sync_logger():
    """Prevent sync file handlers from leaking between tests and into production logs."""
    yield
    sync_logger = logging.getLogger("app.sync")
    for handler in sync_logger.handlers[:]:
        handler.close()
        sync_logger.removeHandler(handler)


@pytest.fixture(scope="session")
def apply_migrations():
    """Marker fixture — migrations already ran at module import time."""
    yield


@pytest.fixture
def db_session(apply_migrations):
    """Provide a SQLAlchemy session backed by a fresh in-memory SQLite DB.

    Each test gets its own in-memory DB with the full schema (created via
    Base.metadata.create_all). Tests are fully isolated — no cleanup needed.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
