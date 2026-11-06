"""Configure sync logging: file (DEBUG) + console (INFO/DEBUG) + error file (WARNING+).

Each sync run gets its own log files tagged with a timestamp, so runs never overwrite
each other's logs.
"""

import logging
import os


def setup_sync_logging(sync_dir: str, run_tag: str, verbose: bool = False) -> None:
    """Set up three handlers for the root sync logger:
    - File handler: persistent/sync/sync_{run_tag}.log (always DEBUG level)
    - Console handler: INFO level normally, DEBUG with --verbose
    - Error file handler: persistent/sync/sync_errors_{run_tag}.log (WARNING+ only)
    """
    os.makedirs(sync_dir, exist_ok=True)
    log_path = os.path.join(sync_dir, f"sync_{run_tag}.log")
    error_log_path = os.path.join(sync_dir, f"sync_errors_{run_tag}.log")

    root = logging.getLogger("app.sync")
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers from previous setup calls (e.g. heal after bulk)
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # File — always full detail
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Console — page-level only unless --verbose
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console_handler)

    # Error file — warnings and errors only, for quick post-sync review
    error_handler = logging.FileHandler(error_log_path, encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(fmt)
    root.addHandler(error_handler)


# ---------------------------------------------------------------------------
# Retention (S15): persistent/sync/ grew to 725 MB in prod (cron.log 366 MB).
# ---------------------------------------------------------------------------

LOG_RETENTION_DAYS = 14
CRON_LOG_MAX_BYTES = 20 * 1024 * 1024
CRON_LOG_KEEP_BYTES = 5 * 1024 * 1024
_PRUNABLE_PREFIXES = ("sync_", "failed_books_")
_PRUNABLE_SUFFIXES = (".log", ".jsonl")


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("SYNC_LOG_RETENTION_DAYS", LOG_RETENTION_DAYS)))
    except ValueError:
        return LOG_RETENTION_DAYS


def prune_sync_logs(
    sync_dir: str,
    *,
    keep_days: int | None = None,
    cron_log_max_bytes: int = CRON_LOG_MAX_BYTES,
    cron_log_keep_bytes: int = CRON_LOG_KEEP_BYTES,
    now: float | None = None,
) -> dict[str, int]:
    """Delete per-run sync files older than ``keep_days`` and cap cron.log.

    - ``sync_*.log``, ``sync_errors_*.log`` and ``failed_books_*.jsonl``
      older than ``keep_days`` (default 14, env SYNC_LOG_RETENTION_DAYS) by
      mtime are deleted.
    - ``cron.log`` above ``cron_log_max_bytes``: its last
      ``cron_log_keep_bytes`` are saved to ``cron.log.1`` (replacing it) and
      cron.log is truncated in place. Cron appends with ``>>`` (O_APPEND), so
      a writer that already has the file open simply continues at the new end.

    Called by every ``python -m app.sync`` CLI run and at container boot.
    Never raises for a single file; returns counts.
    """
    import time

    stats = {"deleted": 0, "freed_bytes": 0, "cron_log_truncated": 0}
    if not os.path.isdir(sync_dir):
        return stats
    days = keep_days if keep_days is not None else _retention_days()
    cutoff = (now if now is not None else time.time()) - days * 86400

    for name in os.listdir(sync_dir):
        if not (name.startswith(_PRUNABLE_PREFIXES) and name.endswith(_PRUNABLE_SUFFIXES)):
            continue
        path = os.path.join(sync_dir, name)
        try:
            st = os.stat(path)
            if st.st_mtime < cutoff:
                os.remove(path)
                stats["deleted"] += 1
                stats["freed_bytes"] += st.st_size
        except OSError:
            continue

    cron_log = os.path.join(sync_dir, "cron.log")
    try:
        size = os.path.getsize(cron_log)
    except OSError:
        size = 0
    if size > cron_log_max_bytes:
        try:
            with open(cron_log, "rb") as src:
                src.seek(max(0, size - cron_log_keep_bytes))
                tail = src.read()
            # Start the kept tail at a line boundary.
            nl = tail.find(b"\n")
            if 0 <= nl < len(tail) - 1:
                tail = tail[nl + 1:]
            with open(cron_log + ".1", "wb") as dst:
                dst.write(tail)
            os.truncate(cron_log, 0)
            stats["cron_log_truncated"] = 1
            stats["freed_bytes"] += size - len(tail)
        except OSError:
            pass
    return stats
