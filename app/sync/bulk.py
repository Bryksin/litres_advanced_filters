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
from app.db.models import Genre, SyncConfig, SyncRun
from app.scrapers.catalog import fetch_catalog_page
from app.scrapers.client import RateLimitedClient
from app.sync.common import (
    GENRE_ID,
    RETRY_WAITS,
    check_no_running_sync,
    commit_with_retry,
    finalise_sync_run,
    get_sync_config,
    log_failed_book,
    make_run_tag,
)
from app.sync.ingest import ingest_book, link_book_series, tag_root_genre

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SyncRun helpers
# ---------------------------------------------------------------------------


def _open_sync_run(
    session: Session, config: SyncConfig, resume: bool, sync_type: str = "bulk",
) -> SyncRun:
    """Create a new SyncRun or resume the latest interrupted one."""
    if resume:
        interrupted = (
            session.query(SyncRun)
            .filter(
                SyncRun.type.in_(["bulk", "delta"]),
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
        type=sync_type,
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


def _retryable_reason(exc: Exception) -> str | None:
    """Return a short reason if a catalog-page error is worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code >= 500 or code == 429:
            return str(code)
        return None
    if isinstance(exc, httpx.TransportError):
        return type(exc).__name__
    return None


def _retry_after_seconds(exc: Exception) -> int:
    """Honour a numeric Retry-After header (429/503), capped at 15 minutes."""
    if isinstance(exc, httpx.HTTPStatusError):
        value = exc.response.headers.get("retry-after", "")
        if value.isdigit():
            return min(int(value), 900)
    return 0


def _refresh_genre_tree(session: Session, client: RateLimitedClient) -> None:
    """Re-scrape the genre DAG before a full crawl (S2). Never fatal."""
    from app.sync.genres import sync_genre_tree

    try:
        total = sync_genre_tree(session, client)
        commit_with_retry(session, context="genre tree refresh")
        log.info("Genre tree refreshed: %d genres", total)
    except Exception:
        session.rollback()
        log.exception("Genre tree refresh failed — continuing with the stored tree")


def _run_maintenance(
    session: Session,
    client: RateLimitedClient,
    run_id: int,
    refresh_series: int,
    refresh_books: int,
) -> None:
    """Bounded roster + stale-book refresh after a successful crawl. Never fatal."""
    from app.sync.refresh import refresh_series_rosters, refresh_stale_books

    try:
        refresh_series_rosters(session, client, limit=refresh_series, run_id=run_id)
    except Exception:
        session.rollback()
        log.exception("Roster refresh failed")
    try:
        refresh_stale_books(session, client, limit=refresh_books, run_id=run_id)
    except Exception:
        session.rollback()
        log.exception("Stale-book refresh failed")


def _adopt_sync_run(session: Session, run_id: int) -> SyncRun:
    """Take over a pre-inserted ``running`` catalog SyncRun (W13).

    Raises if the row is missing, not running, not bulk/delta, or if another
    catalog run is also running.
    """
    from app.sync.common import find_running_catalog_sync

    run = session.get(SyncRun, run_id)
    if run is None or run.status != "running" or run.type not in ("bulk", "delta"):
        raise RuntimeError(f"Cannot adopt SyncRun id={run_id}: not a running bulk/delta row")
    other = find_running_catalog_sync(session, exclude_id=run_id)
    if other is not None:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = f"Not started: SyncRun id={other.id} already running"
        session.commit()
        raise RuntimeError(f"Sync already running: SyncRun id={other.id} started at {other.started_at}.")
    log.info("Adopted SyncRun id=%d (type=%s)", run.id, run.type)
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
    refresh_series: int = 0,
    refresh_books: int = 0,
    refresh_genres: bool = False,
    run_id: int | None = None,
) -> None:
    """Run full catalog bulk sync.

    Iterates pages from GENRE_ID catalog (audiobook, ru, subscription=True).
    Commits per page; updates last_page_fetched after each commit.
    Stops early if max_pages or max_hours is reached.

    Maintenance (off by default; the CLI/cron turns it on):
      - refresh_genres: on a full run (no max_pages), re-scrape the genre DAG
        first (S2). Failure is logged, not fatal.
      - refresh_series / refresh_books: after a run that ends ``done``,
        re-read up to N series rosters and re-check up to N stale books (S3, S4).

    run_id: adopt an existing ``running`` bulk/delta SyncRun that the caller
    already inserted (the admin endpoint claims it atomically, so two clicks
    cannot both start a sync, W13). The running-sync guard then ignores that
    row. ``resume`` is ignored when adopting.
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

    # Final state tracked in locals so it survives even if the ingest session
    # gets poisoned by an OperationalError mid-loop. The fresh-session
    # finaliser in `finally` reads from these, not from the in-memory `run`.
    final_status = "running"
    final_error: str | None = None

    with SessionLocal() as session:
        if run_id is not None:
            run = _adopt_sync_run(session, run_id)
            config = get_sync_config(session)
        else:
            check_no_running_sync(session)
            config = get_sync_config(session)
            sync_type = "delta" if max_pages is not None else "bulk"
            run = _open_sync_run(session, config, resume, sync_type=sync_type)
            run_id = run.id
        run.heartbeat_at = datetime.now(timezone.utc)
        if not dry_run:
            session.commit()

        # Resume from the page AFTER the last completed one.
        # last_page_fetched=None means never started → start from 0.
        # last_page_fetched=N means page N was committed → resume from N+1.
        last_fetched = run.last_page_fetched
        start_page = (last_fetched + 1) if last_fetched is not None else 0

        try:
            with RateLimitedClient() as client:
                if refresh_genres and max_pages is None and not dry_run:
                    _refresh_genre_tree(session, client)
                root_genre_id = str(GENRE_ID)
                tag_root = not dry_run and session.get(Genre, root_genre_id) is not None

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
                            final_status = "interrupted"
                            break

                    # Fetch catalog page — retry up to 3 times on 5xx, 429 and
                    # transport errors (timeouts, resets) (S7).
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
                        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                            reason = _retryable_reason(exc)
                            if reason is None:
                                raise  # other 4xx: non-retriable
                            if _wait is None:
                                log.error(
                                    "[page %d] LitRes %s after %d retries — interrupting sync",
                                    page_number, reason, len(_retry_waits),
                                )
                                final_status = "interrupted"
                                final_error = (
                                    f"LitRes {reason} at page {page_number} "
                                    f"({len(_retry_waits)} retries exhausted)"
                                )
                                return
                            wait = max(_wait, _retry_after_seconds(exc))
                            log.warning(
                                "[page %d] LitRes %s — retrying in %ds (attempt %d/%d)",
                                page_number, reason, wait, _attempt + 1, len(_retry_waits),
                            )
                            time.sleep(wait)

                    if not page.books:
                        if offset > 0 and offset >= page.total_count:
                            log.info("[page %d] no books returned — end of catalog", page_number)
                            final_status = "done"
                        else:
                            # S9: an empty page before the advertised end (or a
                            # response with no counters) is a LitRes error or an
                            # API shape change, not the end of the catalog.
                            log.error(
                                "[page %d] empty page at offset=%d but total=%d — interrupting",
                                page_number, offset, page.total_count,
                            )
                            final_status = "interrupted"
                            final_error = (
                                f"Empty catalog page at offset {offset} (total {page.total_count})"
                            )
                        break

                    log.info("[page %d] %d books", page_number, len(page.books))

                    for art in page.books:
                        sp = session.begin_nested()  # savepoint per book
                        try:
                            is_new, detail = ingest_book(session, client, art, dry_run=dry_run)

                            if is_new and not dry_run and detail is not None:
                                new_count += 1
                                # Series failures are isolated per series inside
                                # link_book_series: they never drop the book (S1).
                                n_series, n_members = link_book_series(session, client, art.id, detail)
                                series_count += n_series
                                new_count += n_members
                            elif is_new and dry_run:
                                new_count += 1
                            else:
                                updated_count += 1

                            if tag_root:
                                tag_root_genre(session, art.id, root_genre_id)
                            log.debug(
                                "[book %d] '%s' — %s",
                                art.id, art.title, "new" if is_new else "updated",
                            )
                            sp.commit()
                        except Exception as exc:
                            sp.rollback()
                            failed_count += 1
                            log_failed_book(sync_dir, run_tag, art, page_number, exc)
                            continue
                        if not dry_run:
                            # Commit per book: the SQLite write lock is held only
                            # for one book, not for a whole page of network calls
                            # (web requests now give up after 5 s, see W1).
                            try:
                                session.commit()
                            except Exception as exc:
                                session.rollback()
                                failed_count += 1
                                log_failed_book(sync_dir, run_tag, art, page_number, exc)

                    if not dry_run:
                        run.last_page_fetched = page_number
                        run.pages_fetched = page_number + 1
                        run.books_upserted = new_count + updated_count
                        run.series_fetched = series_count
                        run.heartbeat_at = datetime.now(timezone.utc)
                        commit_with_retry(session, context=f"bulk page {page_number}")

                    page_number += 1

                    # max_pages check
                    if max_pages is not None and page_number >= start_page + max_pages:
                        log.info("[page %d] max_pages=%d reached — stopping", page_number, max_pages)
                        if final_status == "running":
                            final_status = "done"
                        break

                    if page.next_offset is None:
                        final_status = "done"
                        break

                    offset = page.next_offset

                # Only a full crawl that reached the end stamps last_bulk_sync_at
                # (a --max-pages run is partial) (S16).
                if final_status == "done" and not dry_run and max_pages is None:
                    config.last_bulk_sync_at = datetime.now(timezone.utc)
                    commit_with_retry(session, context="bulk last_bulk_sync_at")

                if final_status == "done" and not dry_run and (refresh_series or refresh_books):
                    _run_maintenance(session, client, run_id, refresh_series, refresh_books)

        except KeyboardInterrupt:
            final_status = "interrupted"
            final_error = "Interrupted by user (Ctrl-C)"
            log.info("Interrupted by user")
        except Exception as exc:
            final_status = "failed"
            final_error = str(exc)
            log.exception("Bulk sync failed")
            raise
        finally:
            # Write terminal state in a brand-new session, fully decoupled from
            # the ingest session. If the ingest session is poisoned by a lock
            # error, the cleanup must still succeed.
            try:
                finalise_sync_run(
                    run_id,
                    status=final_status,
                    error_message=final_error,
                    extra={
                        "books_new": new_count,
                        "books_updated": updated_count,
                        "books_failed": failed_count,
                    },
                )
            except Exception:
                # Last-ditch — log but don't shadow the original exception.
                log.exception("finalise_sync_run failed for id=%d", run_id)

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
        refresh_series=args.refresh_series,
        refresh_books=args.refresh_books,
        refresh_genres=not args.no_refresh_genres,
    )


def run_retry_failed_cli(args) -> None:
    """Read failed_books JSONL and retry each entry.

    Each entry gets its own savepoint and commit, so one DB error cannot
    poison the rest of the file (S5). Series links are restored exactly as in
    run_bulk, and the book is tagged with the crawl root genre.
    """
    import glob as globmod
    from app.scrapers.arts import fetch_arts_detail

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
        root_genre_id = str(GENRE_ID)
        tag_root = session.get(Genre, root_genre_id) is not None
        with RateLimitedClient() as client:
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    art_id = record["book_id"]
                    detail = fetch_arts_detail(client, art_id)
                    sp = session.begin_nested()
                    try:
                        ingest_book(session, client, detail.art, arts_detail=detail)
                        link_book_series(session, client, art_id, detail)
                        if tag_root:
                            tag_root_genre(session, art_id, root_genre_id)
                        sp.commit()
                    except Exception:
                        sp.rollback()
                        raise
                    commit_with_retry(session, context=f"retry-failed book {art_id}")
                    successes.append(i)
                    log.info("Retried book %d: OK", art_id)
                except Exception as exc:
                    session.rollback()
                    failures.append(line)
                    log.error("Retry failed for book: %s — %s", line[:80], exc)

    # Rewrite file with only the still-failing entries
    with open(failed_file, "w", encoding="utf-8") as f:
        for line in failures:
            f.write(line + "\n")

    print(f"Retry done: {len(successes)} succeeded, {len(failures)} still failing")
