"""Tests for app.sync.logging_setup — per-run log files."""

import logging
import os

from app.sync.logging_setup import setup_sync_logging

TAG = "test_run"


def _reset_sync_logger():
    root = logging.getLogger("app.sync")
    for h in root.handlers[:]:
        h.close()
        root.removeHandler(h)


def test_error_log_file_created(tmp_path):
    """setup_sync_logging() must create a sync_errors_{tag}.log handler at WARNING level."""
    _reset_sync_logger()
    setup_sync_logging(str(tmp_path), TAG)

    error_log_path = os.path.join(str(tmp_path), f"sync_errors_{TAG}.log")
    root = logging.getLogger("app.sync")

    error_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.FileHandler)
        and h.baseFilename == error_log_path
    ]
    assert len(error_handlers) == 1
    assert error_handlers[0].level == logging.WARNING


def test_error_log_captures_warnings_and_errors(tmp_path):
    """WARNING and ERROR appear in sync_errors log; DEBUG/INFO do not."""
    _reset_sync_logger()
    setup_sync_logging(str(tmp_path), TAG)

    error_log_path = os.path.join(str(tmp_path), f"sync_errors_{TAG}.log")
    child = logging.getLogger("app.sync.test_child")
    child.debug("debug-msg-xyz")
    child.info("info-msg-xyz")
    child.warning("warning-msg-xyz")
    child.error("error-msg-xyz")

    for h in logging.getLogger("app.sync").handlers:
        h.flush()

    content = open(error_log_path, encoding="utf-8").read()
    assert "warning-msg-xyz" in content
    assert "error-msg-xyz" in content
    assert "debug-msg-xyz" not in content
    assert "info-msg-xyz" not in content


def test_main_log_still_has_all_levels(tmp_path):
    """The main sync log must still capture DEBUG-level messages."""
    _reset_sync_logger()
    setup_sync_logging(str(tmp_path), TAG)

    main_log_path = os.path.join(str(tmp_path), f"sync_{TAG}.log")
    child = logging.getLogger("app.sync.test_child2")
    child.debug("debug-regression-check")
    child.warning("warning-regression-check")

    for h in logging.getLogger("app.sync").handlers:
        h.flush()

    content = open(main_log_path, encoding="utf-8").read()
    assert "debug-regression-check" in content
    assert "warning-regression-check" in content


def test_handler_count_is_three(tmp_path):
    """setup_sync_logging() should create exactly 3 handlers."""
    _reset_sync_logger()
    setup_sync_logging(str(tmp_path), TAG)

    root = logging.getLogger("app.sync")
    assert len(root.handlers) == 3
