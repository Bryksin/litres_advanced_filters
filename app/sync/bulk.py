"""Bulk sync engine — scans all catalog pages from page 0 to last.

Crash-safe: SyncRun.status is always written in try/finally.
Resume: --resume reads last_page_fetched from the latest interrupted SyncRun.
Time-boxed: --max-hours stops after N hours and sets status='interrupted'.
Error isolation: each book wrapped in a SQLite savepoint; failures logged to failed_books.jsonl.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config.config import Config
from app.db import SessionLocal
from app.db.models import Series, SyncConfig, SyncRun
from app.scrapers.catalog import fetch_catalog_page
from app.scrapers.client import RateLimitedClient
from app.sync.common import (
    GENRE_ID,
    RETRY_WAITS,
    check_no_running_sync,
    get_sync_config,
    log_failed_book,
    make_run_tag,
)
from app.sync.ingest import _upsert_book_series_link, ingest_book, ingest_series

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SyncRun helpers
# ---------------------------------------------------------------------------


def _open_sync_run(session: Session, config: SyncConfig, resume: bool) -> SyncRun:
    """Create a new SyncRun or resume the latest interrupted one."""
    if resume:
        interrupted = (
            session.query(SyncRun)
            .filter(
                SyncRun.type == "bulk",
                SyncRun.status.in_(["interrupted", "failed"]),
                SyncRun.last_page_fetched.isnot(None),
            )
            .order_by(SyncRun.started_at.desc())
            .first()
        )
        if interrupted:
            log.info(
                "Resuming SyncRun id=%d (status=%s) from page %d",
                interrupted.id,
                interrupted.status,
                interrupted.last_page_fetched or 0,
            )
            interrupted.status = "running"
            session.commit()
            return interrupted

    run = SyncRun(
        sync_config_id=config.id,
        type="bulk",
        started_at=datetime.now(timezone.utc),
        status="running",
        pages_fetched=0,
        books_upserted=0,
        series_fetched=0,
    )
    session.add(run)
    session.commit()
    log.info("Started new SyncRun id=%d", run.id)
    return run


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


def run_bulk(
    *,
    resume: bool = False,
    max_pages: int | None = None,
    max_hours: float | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Run full catalog bulk sync.

    Iterates pages from GENRE_ID catalog (audiobook, ru, subscription=True).
    Commits per page; updates last_page_fetched after each commit.
    Stops early if max_pages or max_hours is reached.
    """
    from app.sync.logging_setup import setup_sync_logging
    run_tag = make_run_tag()
    setup_sync_logging(Config.SYNC_DIR, run_tag, verbose=verbose)

    sync_dir = Config.SYNC_DIR
    start_time = time.monotonic()
    new_count = 0
    updated_count = 0
    series_count = 0
    failed_count = 0

    with SessionLocal() as session:
        check_no_running_sync(session)
        config = get_sync_config(session)
        run = _open_sync_run(session, config, resume)

        # Resume from the page AFTER the last completed one.
        # last_page_fetched=None means never started → start from 0.
        # last_page_fetched=N means page N was committed → resume from N+1.
        last_fetched = run.last_page_fetched
        start_page = (last_fetched + 1) if last_fetched is not None else 0

        try:
            with RateLimitedClient() as client:
                offset = start_page * 24
                page_number = start_page

                while True:
                    # Time-box check
                    if max_hours is not None:
                        elapsed_hours = (time.monotonic() - start_time) / 3600
                        if elapsed_hours >= max_hours:
                            log.info(
                                "[page %d] max_hours=%.1f reached (elapsed=%.2fh) — stopping",
                                page_number,
                                max_hours,
                                elapsed_hours,
                            )
                            run.status = "interrupted"
                            break

                    # Fetch catalog page — retry up to 3 times on 5xx.
                    # Retry waits: 60s / 300s / 600s. On exhaustion: interrupt (not fail).
                    _retry_waits = RETRY_WAITS
                    page = None
                    for _attempt, _wait in enumerate(_retry_waits + [None]):
                        try:
                            log.info("[page %d] fetching offset=%d...", page_number, offset)
                            page = fetch_catalog_page(
                                client,
                                GENRE_ID,
                                offset=offset,
                                only_litres_subscription_arts=True,
                                o="new",
                            )
                            break
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code < 500:
                                raise  # 4xx: non-retriable
                            if _wait is None:
                                log.error(
                                    "[page %d] LitRes %d after %d retries — interrupting sync",
                                    page_number, exc.response.status_code, len(_retry_waits),
                                )
                                run.status = "interrupted"
                                run.error_message = (
                                    f"LitRes {exc.response.status_code} at page {page_number} "
                                    f"({len(_retry_waits)} retries exhausted)"
                                )
                                return
                            log.warning(
                                "[page %d] LitRes %d — retrying in %ds (attempt %d/%d)",
                                page_number, exc.response.status_code,
                                _wait, _attempt + 1, len(_retry_waits),
                            )
                            time.sleep(_wait)

                    if not page.books:
                        log.info("[page %d] no books returned — end of catalog", page_number)
                        run.status = "done"
                        break

                    log.info("[page %d] %d books", page_number, len(page.books))

                    for art in page.books:
                        sp = session.begin_nested()  # savepoint per book
                        try:
                            is_new, detail = ingest_book(session, client, art, dry_run=dry_run)

                            if is_new and not dry_run and detail is not None:
                                new_count += 1
                                for s_ref in detail.series:
                                    if s_ref.art_order is None:
                                        continue  # skip author collections
                                    existing_series = session.get(Series, s_ref.id)
                                    if existing_series is None:
                                        log.info(
                                            "[book %d] new series '%s' (id=%d, %d books) — ingesting",
                                            art.id, s_ref.name, s_ref.id, s_ref.unique_arts_count,
                                        )
                                        ingested = ingest_series(
                                            session, client,
                                            s_ref.id, s_ref.name, s_ref.url, s_ref.unique_arts_count,
                                            dry_run=dry_run,
                                        )
                                        series_count += 1
                                        new_count += ingested
                                        # Always link the triggering book — ingest_series may
                                        # have returned 0 if the series page filtered it out.
                                        if not dry_run:
                                            _upsert_book_series_link(
                                                session, art.id, s_ref.id, s_ref.art_order
                                            )
                                    else:
                                        _upsert_book_series_link(
                                            session, art.id, s_ref.id, s_ref.art_order
                                        )
                            elif is_new and dry_run:
                                new_count += 1
                            else:
                                updated_count += 1

                            log.debug(
                                "[book %d] '%s' — %s",
                                art.id, art.title, "new" if is_new else "updated",
                            )
                            sp.commit()
                        except Exception as exc:
                            sp.rollback()
                            failed_count += 1
                            log_failed_book(sync_dir, run_tag, art, page_number, exc)

                    if not dry_run:
                        run.last_page_fetched = page_number
                        run.pages_fetched = page_number + 1
                        run.books_upserted = new_count + updated_count
                        run.series_fetched = series_count
                        session.commit()

                    page_number += 1

                    # max_pages check
                    if max_pages is not None and page_number >= start_page + max_pages:
                        log.info("[page %d] max_pages=%d reached — stopping", page_number, max_pages)
                        if run.status == "running":
                            run.status = "done"
                        break

                    if page.next_offset is None:
                        run.status = "done"
                        break

                    offset = page.next_offset

                if run.status == "done" and not dry_run:
                    config.last_bulk_sync_at = datetime.now(timezone.utc)

        except KeyboardInterrupt:
            run.status = "interrupted"
            run.error_message = "Interrupted by user (Ctrl-C)"
            log.info("Interrupted by user")
        except Exception as exc:
            run.status = "failed"
            run.error_message = str(exc)
            log.exception("Bulk sync failed")
            raise
        finally:
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
            final_status = run.status  # capture before session closes

    elapsed = time.monotonic() - start_time
    summary = (
        f"Sync {final_status}: {new_count} new, {updated_count} updated, "
        f"{series_count} series, {failed_count} failed. "
        f"Duration: {elapsed / 60:.0f}m {elapsed % 60:.0f}s"
    )
    log.info(summary)
    print(summary)


# ---------------------------------------------------------------------------
# CLI wrappers
# ---------------------------------------------------------------------------


def run_bulk_cli(args) -> None:
    run_bulk(
        resume=args.resume,
        max_pages=args.max_pages,
        max_hours=args.max_hours,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


def run_retry_failed_cli(args) -> None:
    """Read failed_books JSONL and retry each entry."""
    import glob as globmod
    from app.scrapers.arts import fetch_arts_detail
    from app.sync.ingest import ingest_book

    if args.failed_file:
        failed_file = args.failed_file
    else:
        # Find the most recent failed_books_*.jsonl in sync dir
        pattern = os.path.join(Config.SYNC_DIR, "failed_books_*.jsonl")
        candidates = sorted(globmod.glob(pattern))
        if not candidates:
            print(f"No failed_books files found in {Config.SYNC_DIR}")
            return
        failed_file = candidates[-1]  # latest by timestamp in filename

    if not os.path.exists(failed_file):
        print(f"No failed books file at {failed_file}")
        return

    print(f"Retrying from: {failed_file}")
    with open(failed_file, encoding="utf-8") as f:
        lines = f.readlines()

    successes = []
    failures = []

    with SessionLocal() as session:
        with RateLimitedClient() as client:
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    art_id = record["book_id"]
                    detail = fetch_arts_detail(client, art_id)
                    ingest_book(session, client, detail.art, arts_detail=detail)
                    session.commit()
                    successes.append(i)
                    log.info("Retried book %d: OK", art_id)
                except Exception as exc:
                    failures.append(line)
                    log.error("Retry failed for book: %s — %s", line[:80], exc)

    # Rewrite file with only the still-failing entries
    with open(failed_file, "w", encoding="utf-8") as f:
        for line in failures:
            f.write(line + "\n")

    print(f"Retry done: {len(successes)} succeeded, {len(failures)} still failing")
