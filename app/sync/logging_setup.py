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
