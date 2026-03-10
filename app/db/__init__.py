"""Database package — engine, session, and ORM models.

Usage:
    from app.db import Base, engine, SessionLocal
    from app.db.models import Book, Series, Genre, Person, ...

Importing this package ensures all models are registered on Base.metadata,
which is required for Alembic autogenerate and Base.metadata.create_all().
"""

from app.db.base import Base, SessionLocal, engine
from app.db import models  # noqa: F401 — registers all models on Base.metadata

__all__ = ["Base", "engine", "SessionLocal", "models"]
