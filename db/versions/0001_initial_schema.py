"""Initial schema — all tables.

Revision ID: 0001
Revises:
Create Date: 2026-03-03
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- genre (self-referential tree) -------------------------------------------
    op.create_table(
        "genre",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("genre.id"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("count", sa.Integer, nullable=True),
    )

    # --- person (authors + narrators) --------------------------------------------
    op.create_table(
        "person",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("slug", sa.String(255), nullable=True),
    )

    # --- book --------------------------------------------------------------------
    op.create_table(
        "book",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("art_type", sa.SmallInteger, nullable=False),
        sa.Column("cover_url", sa.String(512), nullable=True),
        sa.Column("language_code", sa.String(8), nullable=True),
        sa.Column("rating_avg", sa.Float, nullable=True),
        sa.Column("rating_count", sa.Integer, nullable=True),
        sa.Column("is_available_with_subscription", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("is_abonement_art", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("release_date", sa.DateTime, nullable=True),
        sa.Column("cached_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_book_art_type", "book", ["art_type"])
    op.create_index("ix_book_language_code", "book", ["language_code"])
    op.create_index("ix_book_rating_avg", "book", ["rating_avg"])
    op.create_index("ix_book_rating_count", "book", ["rating_count"])
    op.create_index("ix_book_release_date", "book", ["release_date"])
    op.create_index("ix_book_is_available_with_subscription", "book", ["is_available_with_subscription"])

    # --- series ------------------------------------------------------------------
    op.create_table(
        "series",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("slug", sa.String(255), nullable=True),
        sa.Column("url", sa.String(512), nullable=True),
        sa.Column("book_count", sa.Integer, nullable=True),
    )

    # --- sync_config (one row; fixed sync scope) ---------------------------------
    # genre_id has no FK — genre table is populated later by 'sync genres'.
    op.create_table(
        "sync_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("genre_slug", sa.String(255), nullable=False),
        sa.Column("genre_id", sa.Integer, nullable=False),
        sa.Column("art_type", sa.String(32), nullable=False),
        sa.Column("language_code", sa.String(8), nullable=False),
        sa.Column("only_subscription", sa.Boolean, nullable=False),
        sa.Column("last_bulk_sync_at", sa.DateTime, nullable=True),
        sa.Column("last_delta_sync_at", sa.DateTime, nullable=True),
    )

    # --- user --------------------------------------------------------------------
    op.create_table(
        "user",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("litres_login", sa.String(255), nullable=True),
        sa.Column("session_data", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    # --- book_genre (book ↔ genre junction) --------------------------------------
    op.create_table(
        "book_genre",
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), primary_key=True),
        sa.Column("genre_id", sa.String(32), sa.ForeignKey("genre.id"), primary_key=True),
        sa.Column("cached_at", sa.DateTime, nullable=False),
    )

    # --- book_author (book ↔ person, with sort_order) ----------------------------
    op.create_table(
        "book_author",
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), primary_key=True),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("person.id"), primary_key=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )

    # --- book_narrator (book ↔ person/narrator) ----------------------------------
    op.create_table(
        "book_narrator",
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), primary_key=True),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("person.id"), primary_key=True),
    )

    # --- book_series (book ↔ series, with position) ------------------------------
    op.create_table(
        "book_series",
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), primary_key=True),
        sa.Column("series_id", sa.Integer, sa.ForeignKey("series.id"), primary_key=True),
        sa.Column("position_in_series", sa.Integer, nullable=True),
    )

    # --- sync_run (one row per sync execution) -----------------------------------
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("sync_config_id", sa.Integer, sa.ForeignKey("sync_config.id"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pages_fetched", sa.Integer, nullable=False, server_default="0"),
        sa.Column("books_upserted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("series_fetched", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_page_fetched", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
    )

    # --- user_ignored_book (F-9 ignore list) ------------------------------------
    op.create_table(
        "user_ignored_book",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), primary_key=True),
        sa.Column("ignored_at", sa.DateTime, nullable=False),
    )

    # --- user_listened_book (F-5 / F-11, v1.1) ----------------------------------
    op.create_table(
        "user_listened_book",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("book_id", sa.Integer, sa.ForeignKey("book.id"), primary_key=True),
        sa.Column("listened_at", sa.DateTime, nullable=False),
    )

    # --- user_settings (persisted filter state) ----------------------------------
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("genre_id", sa.String(32), sa.ForeignKey("genre.id"), nullable=True),
        sa.Column("series_only", sa.Boolean, nullable=True),
        sa.Column("standalones_only", sa.Boolean, nullable=True),
        sa.Column("exclude_authors", sa.Boolean, nullable=True),
        sa.Column("excluded_authors_json", sa.Text, nullable=True),
        sa.Column("rating_min", sa.Float, nullable=True),
        sa.Column("rating_max", sa.Float, nullable=True),
        sa.Column("exclude_narrators", sa.Boolean, nullable=True),
        sa.Column("excluded_narrators_json", sa.Text, nullable=True),
        sa.Column("series_min", sa.Integer, nullable=True),
        sa.Column("series_max", sa.Integer, nullable=True),
        sa.Column("full_series_subscription", sa.Boolean, nullable=True),
        sa.Column("hide_listened", sa.Boolean, nullable=True),
        sa.Column("incomplete_series_only", sa.Boolean, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )

    # --- Seed: fixed sync scope --------------------------------------------------
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    op.execute(sa.text(
        "INSERT INTO sync_config "
        "(id, genre_slug, genre_id, art_type, language_code, only_subscription, "
        "last_bulk_sync_at, last_delta_sync_at) "
        "VALUES (1, 'legkoe-chtenie', 201583, 'audiobook', 'ru', 1, NULL, NULL)"
    ))

    # --- Seed: default anonymous user (v1, no auth) ------------------------------
    op.execute(sa.text(
        f"INSERT INTO \"user\" (id, litres_login, session_data, created_at) "
        f"VALUES (1, NULL, NULL, '{now}')"
    ))
    op.execute(sa.text(
        f"INSERT INTO user_settings (user_id, updated_at) "
        f"VALUES (1, '{now}')"
    ))


def downgrade() -> None:
    # Drop in reverse FK dependency order.
    op.drop_table("user_settings")
    op.drop_table("user_listened_book")
    op.drop_table("user_ignored_book")
    op.drop_table("sync_run")
    op.drop_table("book_series")
    op.drop_table("book_narrator")
    op.drop_table("book_author")
    op.drop_table("book_genre")
    op.drop_table("user")
    op.drop_table("sync_config")
    op.drop_table("series")
    op.drop_index("ix_book_is_available_with_subscription", "book")
    op.drop_index("ix_book_release_date", "book")
    op.drop_index("ix_book_rating_count", "book")
    op.drop_index("ix_book_rating_avg", "book")
    op.drop_index("ix_book_language_code", "book")
    op.drop_index("ix_book_art_type", "book")
    op.drop_table("book")
    op.drop_table("person")
    op.drop_table("genre")
