"""SQLAlchemy ORM models — mirrors docs/db_schema.puml exactly.

Import order follows FK dependency graph so Alembic autogenerate is stable.
All relationships are defined with back_populates for explicit bidirectionality.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ---------------------------------------------------------------------------
# Leaf / independent tables
# ---------------------------------------------------------------------------


class Genre(Base):
    """LitRes genre tree node. Self-referential (parent_id → id)."""

    __tablename__ = "genre"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("genre.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    parent: Mapped["Genre | None"] = relationship("Genre", remote_side="Genre.id", back_populates="children")
    children: Mapped[list["Genre"]] = relationship("Genre", back_populates="parent")
    book_genres: Mapped[list["BookGenre"]] = relationship("BookGenre", back_populates="genre")
    user_settings: Mapped[list["UserSettings"]] = relationship("UserSettings", back_populates="genre")


class Person(Base):
    """Author or narrator (reader). Role is implied by which junction table links them."""

    __tablename__ = "person"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)

    book_author_rows: Mapped[list["BookAuthor"]] = relationship("BookAuthor", back_populates="person")
    book_narrator_rows: Mapped[list["BookNarrator"]] = relationship("BookNarrator", back_populates="person")


class Book(Base):
    """Core book/art entity. art_type: 0=text, 1=audiobook."""

    __tablename__ = "book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    art_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, index=True)
    cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    rating_avg: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    rating_count: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    is_available_with_subscription: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    is_abonement_art: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    release_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    book_genres: Mapped[list["BookGenre"]] = relationship("BookGenre", back_populates="book")
    book_authors: Mapped[list["BookAuthor"]] = relationship(
        "BookAuthor", back_populates="book", order_by="BookAuthor.sort_order"
    )
    book_narrators: Mapped[list["BookNarrator"]] = relationship("BookNarrator", back_populates="book")
    book_series_rows: Mapped[list["BookSeries"]] = relationship("BookSeries", back_populates="book")
    ignored_by: Mapped[list["UserIgnoredBook"]] = relationship("UserIgnoredBook", back_populates="book")
    listened_by: Mapped[list["UserListenedBook"]] = relationship("UserListenedBook", back_populates="book")


class Series(Base):
    """Book series / collection."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    book_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    book_series_rows: Mapped[list["BookSeries"]] = relationship("BookSeries", back_populates="series")


class SyncConfig(Base):
    """Fixed sync scope configuration — one row, seeded in migration.

    genre_id has no FK constraint: the genre table is populated by 'sync genres'
    (Phase 4), which runs after the initial migration. Storing as a plain integer
    avoids a migration dependency on the genre tree.
    """

    __tablename__ = "sync_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    genre_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    genre_id: Mapped[int] = mapped_column(Integer, nullable=False)  # no FK — see docstring
    art_type: Mapped[str] = mapped_column(String(32), nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    only_subscription: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_bulk_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delta_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sync_runs: Mapped[list["SyncRun"]] = relationship("SyncRun", back_populates="sync_config")


class User(Base):
    """App user. v1 = single user; v1.1 = multi-user with LitRes login."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    litres_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    ignored_books: Mapped[list["UserIgnoredBook"]] = relationship("UserIgnoredBook", back_populates="user")
    listened_books: Mapped[list["UserListenedBook"]] = relationship("UserListenedBook", back_populates="user")
    settings: Mapped["UserSettings | None"] = relationship(
        "UserSettings", back_populates="user", uselist=False
    )


# ---------------------------------------------------------------------------
# Junction / dependent tables
# ---------------------------------------------------------------------------


class BookGenre(Base):
    """Book ↔ Genre junction. Tracks which genre(s) a book belongs to.

    A book can appear in multiple LitRes genre trees.
    """

    __tablename__ = "book_genre"
    __table_args__ = (
        Index("ix_book_genre_genre_id", "genre_id"),
    )

    book_id: Mapped[int] = mapped_column(Integer, ForeignKey("book.id"), primary_key=True)
    genre_id: Mapped[str] = mapped_column(String(32), ForeignKey("genre.id"), primary_key=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    book: Mapped["Book"] = relationship("Book", back_populates="book_genres")
    genre: Mapped["Genre"] = relationship("Genre", back_populates="book_genres")


class BookAuthor(Base):
    """Book ↔ Person (author) junction.

    sort_order=0 is the first/main author (used for "First Author et al." display).
    """

    __tablename__ = "book_author"
    __table_args__ = (
        UniqueConstraint("book_id", "person_id"),
        Index("ix_book_author_person_id", "person_id"),
    )

    book_id: Mapped[int] = mapped_column(Integer, ForeignKey("book.id"), primary_key=True)
    person_id: Mapped[int] = mapped_column(Integer, ForeignKey("person.id"), primary_key=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    book: Mapped["Book"] = relationship("Book", back_populates="book_authors")
    person: Mapped["Person"] = relationship("Person", back_populates="book_author_rows")


class BookNarrator(Base):
    """Book ↔ Person (narrator/reader) junction.

    One book can have multiple narrators (e.g. full-cast audio).
    LitRes API: persons[].role == 'reader'.
    """

    __tablename__ = "book_narrator"
    __table_args__ = (
        UniqueConstraint("book_id", "person_id"),
        Index("ix_book_narrator_person_id", "person_id"),
    )

    book_id: Mapped[int] = mapped_column(Integer, ForeignKey("book.id"), primary_key=True)
    person_id: Mapped[int] = mapped_column(Integer, ForeignKey("person.id"), primary_key=True)

    book: Mapped["Book"] = relationship("Book", back_populates="book_narrators")
    person: Mapped["Person"] = relationship("Person", back_populates="book_narrator_rows")


class BookSeries(Base):
    """Book ↔ Series junction with position.

    EXISTS(book_series WHERE book_id=X) → series card in UI; else → book card.
    position_in_series = N in "N of M books in series …".
    Many-to-many: LitRes API returns series as an array per art object.
    """

    __tablename__ = "book_series"
    __table_args__ = (
        Index("ix_book_series_series_id", "series_id"),
    )

    book_id: Mapped[int] = mapped_column(Integer, ForeignKey("book.id"), primary_key=True)
    series_id: Mapped[int] = mapped_column(Integer, ForeignKey("series.id"), primary_key=True)
    position_in_series: Mapped[int | None] = mapped_column(Integer, nullable=True)

    book: Mapped["Book"] = relationship("Book", back_populates="book_series_rows")
    series: Mapped["Series"] = relationship("Series", back_populates="book_series_rows")


class SyncRun(Base):
    """One row per sync execution. Tracks progress and final status.

    status is always written on exit — including on crash — via try/finally.
    last_page_fetched enables --resume for interrupted bulk syncs.
    type: bulk / profile.
    status: running / done / failed / interrupted.
    """

    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_config_id: Mapped[int] = mapped_column(Integer, ForeignKey("sync_config.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    books_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    series_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_page_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    sync_config: Mapped["SyncConfig"] = relationship("SyncConfig", back_populates="sync_runs")


class UserIgnoredBook(Base):
    """Books the user has added to the ignore list (F-9)."""

    __tablename__ = "user_ignored_book"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), primary_key=True)
    book_id: Mapped[int] = mapped_column(Integer, ForeignKey("book.id"), primary_key=True)
    ignored_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="ignored_books")
    book: Mapped["Book"] = relationship("Book", back_populates="ignored_by")


class UserListenedBook(Base):
    """Books the user has listened to — populated from LitRes in v1.1 (F-5, F-11)."""

    __tablename__ = "user_listened_book"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), primary_key=True)
    book_id: Mapped[int] = mapped_column(Integer, ForeignKey("book.id"), primary_key=True)
    listened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="listened_books")
    book: Mapped["Book"] = relationship("Book", back_populates="listened_by")


class UserSettings(Base):
    """Persisted filter state per user. One row per user; upserted on every Apply.

    excluded_narrators_json / excluded_authors_json: JSON arrays of name strings.
    All filter fields are nullable — NULL means "use default / not set".
    """

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), primary_key=True)
    genre_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("genre.id"), nullable=True)
    series_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    standalones_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exclude_authors: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    excluded_authors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    rating_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    exclude_narrators: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    excluded_narrators_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    full_series_subscription: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hide_listened: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    incomplete_series_only: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="settings")
    genre: Mapped["Genre | None"] = relationship("Genre", back_populates="user_settings")
