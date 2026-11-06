"""Catalog blueprint — GET /."""

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, render_template, request

from app.config.config import Config
from app.db import SessionLocal
from app.db.models import BookSeries, Genre, UserIgnoredBook
from app.services.catalog_service import get_catalog
from app.services.genre_service import get_genre_ancestor_ids, get_genre_tree
from app.services.settings_service import (
    DEFAULT_SORT,
    PAGE_MAX,
    RATING_COUNT_MAX,
    SERIES_SIZE_MAX,
    SettingsService,
    clamp_int,
    clamp_rating,
    clean_sort,
    parse_name_list_json,
)

bp = Blueprint("catalog", __name__)


@bp.route("/")
def index():
    with SessionLocal() as session:
        settings_svc = SettingsService(session, user_id=g.user_id)

        # Load saved settings as base
        query = settings_svc.get()

        # F-5/F-11 need the LitRes listened list. An expired-but-known session
        # still counts as authenticated and filters on the cached list.
        from app.controllers.auth import _get_listened_count, _is_authenticated
        is_authenticated, auth_email = _is_authenticated(SessionLocal, g.user_id)

        # A saved genre that no longer exists (removed by a genre reseed) falls back.
        saved_genre_id = query.genre_id if _genre_exists(session, query.genre_id) else None
        query.genre_id = saved_genre_id

        # Override from URL params when filter form was submitted
        if "_submit" in request.args:
            query.genre_id = _valid_genre_id(session, request.args.get("genre_id"), saved_genre_id)
            query.series_only = "only_series" in request.args
            query.standalones_only = "standalones_only" in request.args
            query.series_min = clamp_int(request.args.get("series_min"), 0, SERIES_SIZE_MAX)
            query.series_max = clamp_int(request.args.get("series_max"), 0, SERIES_SIZE_MAX)
            query.full_series_subscription = "full_series_subscription" in request.args
            query.exclude_authors = "exclude_authors" in request.args
            if query.exclude_authors:
                query.excluded_authors = parse_name_list_json(request.args.get("excluded_authors_json"))
            query.exclude_narrators = "exclude_narrators" in request.args
            if query.exclude_narrators:
                query.excluded_narrators = parse_name_list_json(request.args.get("excluded_narrators_json"))
            query.rating_min = clamp_rating(request.args.get("rating_min"))
            query.rating_max = clamp_rating(request.args.get("rating_max"))
            query.rating_count_min = clamp_int(request.args.get("rating_count_min"), 0, RATING_COUNT_MAX)
            # The listened-based checkboxes are disabled in the form while logged
            # out, so their absence then means "not sent", not "off": keep the
            # saved values (Q-S5).
            if is_authenticated:
                query.hide_listened = "hide_listened" in request.args
                query.incomplete_series_only = "incomplete_series_only" in request.args
                # Mutually exclusive: incomplete_series_only wins if both sent
                if query.hide_listened and query.incomplete_series_only:
                    query.hide_listened = False
            # The form omits the sort parameter for the default order.
            query.sort = clean_sort(request.args.get("sort") or DEFAULT_SORT)
            settings_svc.save(query)
        elif "_reset" in request.args:
            query = settings_svc.reset()
        else:
            if request.args.get("genre_id"):
                # Genre navigation (no _submit) — just override genre
                query.genre_id = _valid_genre_id(session, request.args.get("genre_id"), saved_genre_id)
            if request.args.get("sort"):
                # A sort change outside the form is remembered too (Q-S6).
                query.sort = clean_sort(request.args.get("sort"))
                settings_svc.save_sort(query.sort)

        # Pagination from URL (not persisted)
        query.page = clamp_int(request.args.get("page"), 1, PAGE_MAX) or 1

        # The sidebar highlights the default genre when none is saved; filter
        # the catalog by that same genre so the page matches the highlight.
        genre_id = query.genre_id or Config.DEFAULT_GENRE_ID
        query.genre_id = genre_id
        genres = get_genre_tree(session)
        open_genre_ids = get_genre_ancestor_ids(session, genre_id)

        # Disable F-5/F-11 for this request (the saved settings are kept) on
        # every path when the user has no LitRes account.
        if not is_authenticated:
            query.hide_listened = False
            query.incomplete_series_only = False

        result = get_catalog(session, query, user_id=g.user_id)

        ignored_count = (
            session.query(UserIgnoredBook)
            .filter(UserIgnoredBook.user_id == g.user_id)
            .count()
        )

        listened_count = _get_listened_count(SessionLocal, g.user_id) if is_authenticated else 0

        return render_template(
            "catalog.html",
            items=result.cards,
            genres=genres,
            settings=query,
            selected_genre_id=genre_id,
            open_genre_ids=open_genre_ids,
            current_page=result.page,
            has_next=result.has_next,
            total_count=result.total_count,
            ignored_count=ignored_count,
            is_offline=False,
            is_authenticated=is_authenticated,
            auth_email=auth_email,
            listened_count=listened_count,
        )


def _genre_exists(session, genre_id: str | None) -> bool:
    if not genre_id or len(genre_id) > 32:
        return False
    return session.get(Genre, genre_id) is not None


def _valid_genre_id(session, requested: str | None, fallback: str | None) -> str | None:
    """The requested genre when it exists, else the fallback (saved genre or None = default)."""
    if requested and _genre_exists(session, requested):
        return requested
    return fallback


# ---------------------------------------------------------------------------
# Ignore list routes (F-9)
# ---------------------------------------------------------------------------


@bp.route("/ignore/<int:book_id>", methods=["POST"])
def ignore_book(book_id):
    """Add a book to the user's ignore list."""
    try:
        with SessionLocal() as session:
            existing = session.get(UserIgnoredBook, (g.user_id, book_id))
            if not existing:
                session.add(UserIgnoredBook(
                    user_id=g.user_id,
                    book_id=book_id,
                    ignored_at=datetime.now(timezone.utc),
                ))
                session.commit()
        return jsonify({"ok": True, "book_id": book_id})
    except Exception:
        return jsonify({"ok": False, "error": "book_not_found"}), 404


@bp.route("/unignore/<int:book_id>", methods=["POST"])
def unignore_book(book_id):
    """Remove a book from the user's ignore list."""
    with SessionLocal() as session:
        row = session.get(UserIgnoredBook, (g.user_id, book_id))
        if row:
            session.delete(row)
            session.commit()
    return jsonify({"ok": True, "book_id": book_id})


@bp.route("/ignore/series/<int:series_id>", methods=["POST"])
def ignore_series(series_id):
    """Ignore all books in a series."""
    with SessionLocal() as session:
        book_ids = [
            row.book_id
            for row in session.query(BookSeries.book_id)
            .filter(BookSeries.series_id == series_id)
            .all()
        ]
        count = 0
        for bid in book_ids:
            existing = session.get(UserIgnoredBook, (g.user_id, bid))
            if not existing:
                session.add(UserIgnoredBook(
                    user_id=g.user_id,
                    book_id=bid,
                    ignored_at=datetime.now(timezone.utc),
                ))
                count += 1
        session.commit()
    return jsonify({"ok": True, "series_id": series_id, "count": count})


@bp.route("/unignore/series/<int:series_id>", methods=["POST"])
def unignore_series(series_id):
    """Unignore all books in a series."""
    with SessionLocal() as session:
        book_ids = [
            row.book_id
            for row in session.query(BookSeries.book_id)
            .filter(BookSeries.series_id == series_id)
            .all()
        ]
        session.query(UserIgnoredBook).filter(
            UserIgnoredBook.user_id == g.user_id,
            UserIgnoredBook.book_id.in_(book_ids),
        ).delete(synchronize_session="fetch")
        session.commit()
    return jsonify({"ok": True, "series_id": series_id})


@bp.route("/ignore/clear", methods=["POST"])
def clear_ignored():
    """Clear entire ignore list."""
    with SessionLocal() as session:
        session.query(UserIgnoredBook).filter(
            UserIgnoredBook.user_id == g.user_id,
        ).delete(synchronize_session="fetch")
        session.commit()
    return jsonify({"ok": True})
