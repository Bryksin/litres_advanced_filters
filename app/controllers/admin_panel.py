"""Admin panel blueprint — protected by ADMIN_EMAILS allowlist.

Provides the admin dashboard and sync management endpoints.
All routes require the user to be logged into LitRes with an email
listed in the ADMIN_EMAILS environment variable.

GET  /admin/              — admin dashboard
POST /admin/sync/bulk     — start or resume bulk sync in background
POST /admin/sync/profile  — trigger profile sync for current session user
GET  /admin/sync/status   — return latest sync_run as JSON
POST /admin/cron/update   — update and install crontab
POST /admin/sync/retry    — retry failed books from a JSONL file
"""

import glob as globmod
import json as _json
import logging
import os
import re
import subprocess
import threading

from flask import Blueprint, abort, g, jsonify, render_template, request

from app.config import Config
from app.db import SessionLocal
from app.db.models import SyncRun, User

log = logging.getLogger(__name__)
bp = Blueprint("admin_panel", __name__, url_prefix="/admin")

# Crontab paths
_PERSISTENT_CRONTAB = os.path.join(Config.BASE_DIR, "persistent", "crontab")
_DEFAULT_CRONTAB = os.path.join(Config.BASE_DIR, "docker", "app", "crontab")
# In Docker the default lives at /app/crontab; fall back to the repo copy for dev
_DOCKER_CRONTAB = "/app/crontab"


@bp.before_request
def _require_admin():
    """Guard all admin routes behind ADMIN_EMAILS allowlist."""
    # Admin panel disabled entirely when ADMIN_EMAILS is not configured
    if Config.ADMIN_EMAILS is None:
        abort(404)

    # User must be authenticated via LitRes
    with SessionLocal() as s:
        user = s.get(User, g.user_id)
        if not user or not user.litres_login:
            abort(403, description="Войдите через LitRes для доступа к панели администратора")

        # Email must be in the allowlist (case-insensitive)
        if user.litres_login.lower() not in Config.ADMIN_EMAILS:
            abort(403, description="Доступ запрещён")


# ---------------------------------------------------------------------------
# Crontab helpers
# ---------------------------------------------------------------------------

def _crontab_path() -> str | None:
    """Return the path to the active crontab file, or None if not found."""
    if os.path.isfile(_PERSISTENT_CRONTAB):
        return _PERSISTENT_CRONTAB
    if os.path.isfile(_DOCKER_CRONTAB):
        return _DOCKER_CRONTAB
    if os.path.isfile(_DEFAULT_CRONTAB):
        return _DEFAULT_CRONTAB
    return None


def read_crontab() -> list[dict]:
    """Parse the active crontab into a list of job dicts.

    Each dict has: description, schedule, command, raw_line.
    The description is taken from the comment line immediately above the job.
    """
    path = _crontab_path()
    if path is None:
        return []

    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    jobs: list[dict] = []
    prev_comment = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            prev_comment = ""
            continue
        if stripped.startswith("#"):
            # Keep the last comment before a job line as its description
            prev_comment = stripped.lstrip("# ").strip()
            continue

        # Cron job line: 5 schedule fields + command
        parts = stripped.split(None, 5)
        if len(parts) >= 6:
            schedule = " ".join(parts[:5])
            command = parts[5]
            jobs.append({
                "description": prev_comment,
                "schedule": schedule,
                "command": command,
                "raw_line": stripped,
            })
        prev_comment = ""

    return jobs


_CRON_FIELD_RE = re.compile(r"^[\d,\-\*/]+$")


def _validate_cron_expression(expr: str) -> bool:
    """Basic validation: 5 space-separated fields, each matching digits/commas/dashes/stars/slashes."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    return all(_CRON_FIELD_RE.match(f) for f in fields)


# ---------------------------------------------------------------------------
# Failed books helpers
# ---------------------------------------------------------------------------

def read_failed_books(sync_dir: str) -> tuple[list[dict], str | None]:
    """Read the most recent failed_books JSONL file.

    Returns (list_of_records, filename) where filename is just the basename.
    Records have: book_id, title, error (truncated), page_number.
    """
    pattern = os.path.join(sync_dir, "failed_books_*.jsonl")
    candidates = sorted(globmod.glob(pattern))
    if not candidates:
        return [], None

    latest = candidates[-1]
    records: list[dict] = []
    try:
        with open(latest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                    records.append({
                        "book_id": rec.get("book_id", "?"),
                        "title": rec.get("title", "—"),
                        "error": (rec.get("error", ""))[:200],
                        "page_number": rec.get("page_number", "?"),
                    })
                except _json.JSONDecodeError:
                    continue
    except OSError:
        return [], None

    return records, os.path.basename(latest)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _format_duration(started_at, finished_at) -> str:
    """Compute a human-readable duration string."""
    if not started_at or not finished_at:
        return "—"
    delta = (finished_at - started_at).total_seconds()
    secs = int(delta)
    if secs >= 3600:
        return f"{secs // 3600}ч {(secs % 3600) // 60:02d}м"
    return f"{secs // 60}м {secs % 60:02d}с"


@bp.get("/")
def dashboard():
    """Admin dashboard with sync stats, cron, and failed books."""
    with SessionLocal() as session:
        sync_runs = (
            session.query(SyncRun)
            .order_by(SyncRun.started_at.desc())
            .limit(10)
            .all()
        )
        # Detach from session so template can access attrs after session closes
        session.expunge_all()

    last_run = sync_runs[0] if sync_runs else None
    duration_str = _format_duration(
        last_run.started_at, last_run.finished_at
    ) if last_run else "—"

    cron_jobs = read_crontab()
    failed_books, failed_file = read_failed_books(Config.SYNC_DIR)

    return render_template(
        "admin/dashboard.html",
        last_run=last_run,
        duration_str=duration_str,
        sync_runs=sync_runs,
        cron_jobs=cron_jobs,
        failed_books=failed_books,
        failed_file=failed_file or "",
    )


# ---------------------------------------------------------------------------
# Cron update
# ---------------------------------------------------------------------------

@bp.post("/cron/update")
def cron_update():
    """Update and install the crontab from JSON payload."""
    body = request.get_json(silent=True)
    if not body or "jobs" not in body:
        return jsonify({"error": "Missing 'jobs' in request body"}), 400

    jobs = body["jobs"]
    if not isinstance(jobs, list) or not jobs:
        return jsonify({"error": "'jobs' must be a non-empty list"}), 400

    # Validate each job
    for i, job in enumerate(jobs):
        schedule = (job.get("schedule") or "").strip()
        command = (job.get("command") or "").strip()
        if not schedule or not command:
            return jsonify({"error": f"Job {i}: schedule and command are required"}), 400
        if not _validate_cron_expression(schedule):
            return jsonify({"error": f"Job {i}: invalid cron expression '{schedule}'"}), 400

    # Build crontab content
    header = (
        "# LitRes sync schedules (managed by admin panel)\n"
        "#\n"
        "# Edited via /admin/ — changes persist across container restarts\n"
        "\n"
    )
    lines = [header]
    for job in jobs:
        schedule = job["schedule"].strip()
        command = job["command"].strip()
        description = (job.get("description") or "").strip()
        if description:
            lines.append(f"# {description}\n")
        lines.append(f"{schedule}  {command}\n\n")

    content = "".join(lines)

    # Write to persistent location
    os.makedirs(os.path.dirname(_PERSISTENT_CRONTAB), exist_ok=True)
    try:
        with open(_PERSISTENT_CRONTAB, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        log.error("Failed to write crontab: %s", exc)
        return jsonify({"error": f"Failed to write crontab: {exc}"}), 500

    # Install via crontab command
    result = subprocess.run(
        ["crontab", _PERSISTENT_CRONTAB],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("crontab install failed: %s", result.stderr)
        return jsonify({"error": f"crontab install failed: {result.stderr.strip()}"}), 500

    log.info("Crontab updated and installed (%d jobs)", len(jobs))
    return jsonify({"status": "ok", "jobs": len(jobs)}), 200


# ---------------------------------------------------------------------------
# Sync retry
# ---------------------------------------------------------------------------

def _run_retry_background(failed_file_path: str) -> None:
    """Run retry of failed books in a daemon thread."""
    from app.sync.bulk import run_retry_failed_cli
    import argparse

    try:
        # Build a minimal args namespace matching what run_retry_failed_cli expects
        args = argparse.Namespace(
            failed_file=failed_file_path,
            verbose=True,
        )
        run_retry_failed_cli(args)
    except Exception:
        log.exception("Background retry failed")


@bp.post("/sync/retry")
def retry_failed():
    """Retry failed books from a specific JSONL file."""
    body = request.get_json(silent=True) or {}
    filename = body.get("failed_file", "")

    # Validate filename: must match pattern, no path traversal
    if not filename or not re.match(r"^failed_books_\d{8}_\d{6}\.jsonl$", filename):
        return jsonify({"error": "Invalid filename"}), 400

    full_path = os.path.join(Config.SYNC_DIR, filename)
    if not os.path.isfile(full_path):
        return jsonify({"error": "File not found"}), 404

    thread = threading.Thread(
        target=_run_retry_background,
        args=(full_path,),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "file": filename}), 202


# ---------------------------------------------------------------------------
# Sync management (migrated from app/controllers/admin.py)
# ---------------------------------------------------------------------------

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
