"""Admin panel blueprint — protected by ADMIN_EMAILS allowlist.

Provides the admin dashboard and sync management endpoints.
All routes require an ACTIVE (not expired) LitRes session whose email is
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

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request
from sqlalchemy.exc import OperationalError

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
# Env header written by docker/app/entrypoint.sh (cron starts jobs with an
# empty environment, W14). Prepended on every install so an admin edit keeps it.
_CRON_ENV_FILE = "/app/.cron-env"


@bp.before_request
def _require_admin():
    """Guard all admin routes behind ADMIN_EMAILS allowlist."""
    # Admin panel disabled entirely when ADMIN_EMAILS is not configured
    if Config.ADMIN_EMAILS is None:
        abort(404)

    from app.middleware import admin_email_for
    from app.scrapers.auth import has_litres_tokens, parse_session_data

    # User must hold an active LitRes session (an expired or logged-out one,
    # or a bare litres_login, grants nothing — audit W5) ...
    with SessionLocal() as s:
        user = s.get(User, g.user_id)
        if not user or not has_litres_tokens(parse_session_data(user.session_data)):
            abort(403, description="Войдите через LitRes для доступа к панели администратора")

        # ... for an email in the allowlist (case-insensitive)
        if admin_email_for(user, Config.ADMIN_EMAILS) is None:
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
        # Skip environment variable assignments (e.g. PATH=...)
        if re.match(r"^[A-Z_]+=", stripped):
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

# The cron editor may only schedule `python -m app.sync <subcommand> [flags]`
# (audit W5: free-form commands are code execution in the container).
# Optional `cd /app && ` prefix and `>> /app/persistent/sync/<name>.log 2>&1`
# suffix, exactly as in docker/app/crontab.
_CRON_COMMAND_RE = re.compile(
    r"^(?:cd /app && )?"
    r"(?:/usr/local/bin/)?python3? -m app\.sync"
    r"(?P<args>(?: [A-Za-z0-9_./\-]+)+)"
    r"(?: >> (?:/app/)?persistent/sync/[A-Za-z0-9_.\-]+\.log 2>&1)?$"
)
_INT_RE = re.compile(r"^\d{1,6}$")
_FLOAT_RE = re.compile(r"^\d{1,4}(?:\.\d{1,3})?$")
# String-valued flags (e.g. retry-failed --failed-file) may only name a file
# under persistent/sync/
_PATH_RE = re.compile(r"^(?:/app/)?persistent/sync/[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _sync_cli_options() -> dict[str, dict[str, re.Pattern | None]]:
    """{subcommand: {flag: value pattern, or None for a boolean flag}}.

    Derived from the real `python -m app.sync` argparse definition, so the
    allowlist follows the CLI (new flags/subcommands need no change here).
    """
    import argparse

    from app.sync.__main__ import _build_parser

    parser = _build_parser()
    subparsers = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    result: dict[str, dict[str, re.Pattern | None]] = {}
    for name, sp in subparsers.choices.items():
        flags: dict[str, re.Pattern | None] = {}
        for action in sp._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            if action.nargs == 0:
                pattern = None
            elif action.type is int:
                pattern = _INT_RE
            elif action.type is float:
                pattern = _FLOAT_RE
            else:
                pattern = _PATH_RE
            for opt in action.option_strings:
                if opt.startswith("--"):
                    flags[opt] = pattern
        result[name] = flags
    return result


def _validate_cron_command(command: str) -> str | None:
    """Return None if `command` is an allowed sync invocation, else the reason."""
    m = _CRON_COMMAND_RE.match(command)
    if not m:
        return "only 'python -m app.sync <subcommand> [flags]' commands are allowed"
    tokens = m.group("args").split()
    sub, rest = tokens[0], tokens[1:]
    flags = _sync_cli_options().get(sub)
    if flags is None:
        return f"unknown sync subcommand '{sub}'"
    seen: set[str] = set()
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag not in flags:
            return f"flag '{flag}' is not allowed for '{sub}'"
        if flag in seen:
            return f"flag '{flag}' repeated"
        seen.add(flag)
        pattern = flags[flag]
        if pattern is not None:
            if i + 1 >= len(rest) or not pattern.match(rest[i + 1]):
                return f"flag '{flag}' needs a valid value"
            i += 1
        i += 1
    return None


def _single_line(text: str) -> str:
    """Collapse CR/LF and other control characters so a value stays on its line."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


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
    clean_jobs: list[tuple[str, str, str]] = []
    for i, job in enumerate(jobs):
        if not isinstance(job, dict):
            return jsonify({"error": f"Job {i}: must be an object"}), 400
        schedule, command, description = (
            job.get(k) if isinstance(job.get(k), str) else ""
            for k in ("schedule", "command", "description")
        )
        schedule = " ".join(schedule.split())
        command = command.strip()
        if not schedule or not command:
            return jsonify({"error": f"Job {i}: schedule and command are required"}), 400
        if not _validate_cron_expression(schedule):
            return jsonify({"error": f"Job {i}: invalid cron expression '{schedule}'"}), 400
        reason = _validate_cron_command(command)
        if reason:
            return jsonify({"error": f"Job {i}: {reason}"}), 400
        clean_jobs.append((schedule, command, _single_line(description)))

    # Build crontab content
    header = (
        "# LitRes sync schedules (managed by admin panel)\n"
        "#\n"
        "# Edited via /admin/ — changes persist across container restarts\n"
        "\n"
        "PATH=/usr/local/bin:/usr/bin:/bin\n"
        "\n"
    )
    lines = [header]
    for schedule, command, description in clean_jobs:
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

    # Install via crontab command, with the container env header in front
    env_header = ""
    try:
        with open(_CRON_ENV_FILE, encoding="utf-8") as f:
            env_header = f.read()
    except FileNotFoundError:
        pass  # dev / no entrypoint: nothing to prepend
    except OSError as exc:
        log.error("Cannot read %s: %s", _CRON_ENV_FILE, exc)
        return jsonify({"error": f"Cannot read cron env file: {exc}"}), 500
    if env_header and not env_header.endswith("\n"):
        env_header += "\n"
    result = subprocess.run(
        ["crontab", "-"],
        input=env_header + content,
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


def _positive_number(value, kind):
    """None, or a positive int/float; ValueError otherwise (W13)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("max_pages/max_hours must be positive numbers")
    try:
        number = kind(value)
    except (TypeError, ValueError):
        raise ValueError("max_pages/max_hours must be positive numbers") from None
    if kind is int and isinstance(value, float) and value != number:
        raise ValueError("max_pages must be a whole number")
    if number <= 0:
        raise ValueError("max_pages/max_hours must be positive numbers")
    return number


class _SyncAlreadyRunning(Exception):
    def __init__(self, run_id: int) -> None:
        super().__init__(run_id)
        self.run_id = run_id


def _insert_running_catalog_run(sync_type: str) -> int:
    """Insert a ``running`` bulk/delta SyncRun under BEGIN IMMEDIATE (W13).

    The immediate transaction holds the SQLite write lock from the check to
    the commit, so two requests (any worker) cannot both pass the check.
    run_bulk(run_id=...) adopts the row.
    """
    from datetime import datetime, timezone

    from app.sync.common import CATALOG_SYNC_TYPES, get_sync_config

    with SessionLocal() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            other = (
                session.query(SyncRun)
                .filter(SyncRun.status == "running", SyncRun.type.in_(CATALOG_SYNC_TYPES))
                .first()
            )
            if other is not None:
                raise _SyncAlreadyRunning(other.id)
            run = SyncRun(
                sync_config_id=get_sync_config(session).id,
                type=sync_type,
                started_at=datetime.now(timezone.utc),
                heartbeat_at=datetime.now(timezone.utc),
                status="running",
                pages_fetched=0,
                books_upserted=0,
                series_fetched=0,
            )
            session.add(run)
            session.commit()
            return run.id
        except BaseException:
            session.rollback()
            raise


@bp.post("/sync/bulk")
def start_bulk():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    resume = bool(body.get("resume", False))
    dry_run = bool(body.get("dry_run", False))
    try:
        max_pages = _positive_number(body.get("max_pages"), int)
        max_hours = _positive_number(body.get("max_hours"), float)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with SessionLocal() as session:
        # Only live catalog syncs conflict (zombies reaped first); a per-user
        # profile run must not block (W3)
        from app.sync.common import find_running_catalog_sync

        running = find_running_catalog_sync(session)
        if running:
            return jsonify({"error": f"Sync already running (id={running.id})"}), 409

    run_id = None
    if not resume:
        # W13: claim the run inside the request, so a double click or a second
        # worker gets 409 instead of starting a parallel sync. (A resume
        # re-opens an interrupted row inside run_bulk instead.)
        try:
            run_id = _insert_running_catalog_run(
                "delta" if max_pages is not None else "bulk"
            )
        except _SyncAlreadyRunning as exc:
            return jsonify({"error": f"Sync already running (id={exc.run_id})"}), 409
        except OperationalError:
            log.warning("Could not claim a bulk SyncRun: database busy")
            return jsonify({"error": "База данных занята — попробуйте ещё раз через минуту"}), 503

    thread = threading.Thread(
        target=_run_bulk_background,
        kwargs=dict(
            resume=resume, max_pages=max_pages, max_hours=max_hours, dry_run=dry_run, run_id=run_id,
        ),
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

    # Same cross-worker claim as the automatic sync: no duplicate runs (W6)
    from app.sync.profile import claim_profile_sync
    if not claim_profile_sync(
        SessionLocal, g.user_id,
        retry_minutes=current_app.config.get("PROFILE_SYNC_RETRY_MINUTES", 15),
    ):
        return jsonify({
            "error": "Синхронизация профиля уже запущена недавно — попробуйте позже",
        }), 409

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
