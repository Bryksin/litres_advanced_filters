"""Flask admin endpoints for triggering and monitoring sync runs.

POST /admin/sync/bulk    — start or resume bulk sync in a background thread
POST /admin/sync/profile — trigger profile sync for current session user
GET  /admin/sync/status  — return latest sync_run as JSON
"""

import threading
import logging

from flask import Blueprint, jsonify, request

from app.db import SessionLocal
from app.db.models import SyncRun

log = logging.getLogger(__name__)
bp = Blueprint("admin", __name__, url_prefix="/admin")


def _run_bulk_background(**kwargs) -> None:
    """Run bulk sync in a daemon thread. Errors are caught and logged."""
    from app.sync.bulk import run_bulk
    try:
        run_bulk(**kwargs)
    except Exception:
        log.exception("Background bulk sync failed")


@bp.post("/sync/bulk")
def start_bulk():
    body = request.get_json(silent=True) or {}
    resume = bool(body.get("resume", False))
    max_pages = body.get("max_pages")
    max_hours = body.get("max_hours")
    dry_run = bool(body.get("dry_run", False))

    with SessionLocal() as session:
        running = session.query(SyncRun).filter_by(status="running").first()
        if running:
            return jsonify({"error": f"Sync already running (id={running.id})"}), 409

    thread = threading.Thread(
        target=_run_bulk_background,
        kwargs=dict(resume=resume, max_pages=max_pages, max_hours=max_hours, dry_run=dry_run),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "resume": resume, "dry_run": dry_run}), 202


def _run_profile_background(**kwargs) -> None:
    """Run profile sync in a daemon thread."""
    from app.sync.profile import run_profile
    try:
        run_profile(**kwargs)
    except Exception:
        log.exception("Background profile sync failed")


@bp.post("/sync/profile")
def start_profile_sync():
    """Trigger profile sync for the current session user."""
    import json as _json
    from flask import g
    from app.db.models import User

    # Read email from user's stored session_data (set during login)
    with SessionLocal() as s:
        user = s.get(User, g.user_id)
        if not user or not user.session_data:
            return jsonify({"error": "Not authenticated — login first"}), 401
        try:
            data = _json.loads(user.session_data)
            email = data.get("email")
        except (_json.JSONDecodeError, TypeError):
            return jsonify({"error": "Invalid session data"}), 401
        if not email:
            return jsonify({"error": "No email in session — login first"}), 401

    thread = threading.Thread(
        target=_run_profile_background,
        kwargs=dict(
            session_factory=SessionLocal,
            email=email,
            password=None,  # rely on stored tokens; get_valid_token handles refresh
            user_id=g.user_id,
        ),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "user_id": g.user_id}), 202


@bp.get("/sync/status")
def sync_status():
    with SessionLocal() as session:
        latest = (
            session.query(SyncRun)
            .order_by(SyncRun.started_at.desc())
            .first()
        )
        if latest is None:
            return jsonify({"status": "never_run"}), 200
        return jsonify({
            "id": latest.id,
            "type": latest.type,
            "status": latest.status,
            "pages_fetched": latest.pages_fetched,
            "books_upserted": latest.books_upserted,
            "series_fetched": latest.series_fetched,
            "started_at": latest.started_at.isoformat(),
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
            "last_page_fetched": latest.last_page_fetched,
            "error_message": latest.error_message,
        }), 200
