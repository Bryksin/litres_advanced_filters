"""Bounded nightly maintenance run after a successful catalog crawl.

- refresh_series_rosters (S4, S1 retry): re-read series rosters, never-read or
  failed ones first (``roster_synced_at IS NULL``), then the oldest.
- refresh_stale_books (S3): re-check books the crawl has not refreshed for a
  while with one arts call each. This covers books that left the
  subscription or LitRes (the subscription-only crawl no longer returns them)
  and series members outside the crawl's genre, in both flag directions.

Both commit per item (short SQLite write windows next to gunicorn) and bump the
sync_run heartbeat so the zombie reaper sees a live run.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.db.models import Book, Series, SyncRun
from app.scrapers.arts import fetch_arts_detail
from app.scrapers.client import RateLimitedClient
from app.sync.common import commit_with_retry
from app.sync.ingest import MemberBudget, ingest_series, refresh_known_book

log = logging.getLogger(__name__)

# Nightly budgets used by the CLI (cron). At ~1.5 req/s: rosters ~2-4 requests
# each, books 1 request each -> roughly 10-15 minutes per night.
DEFAULT_SERIES_REFRESH = 300
DEFAULT_BOOK_REFRESH = 500
# Arts calls for roster members per night, across all refreshed series. A
# live 1-page test hit one 693-member series (~670 calls), so the series count
# alone does not bound the run.
DEFAULT_MEMBER_CALLS = 1500
# A book refreshed by the crawl within this window is not re-checked. The
# Sunday full crawl refreshes every subscription book in the root genre, so
# 8 days means "not seen by the last full crawl".
STALE_BOOK_AGE = timedelta(days=8)


def _heartbeat(session: Session, run_id: int | None) -> None:
    if run_id is not None:
        session.execute(
            update(SyncRun).where(SyncRun.id == run_id).values(heartbeat_at=datetime.now(timezone.utc))
        )


def refresh_series_rosters(
    session: Session,
    client: RateLimitedClient,
    *,
    limit: int,
    member_calls: int = DEFAULT_MEMBER_CALLS,
    run_id: int | None = None,
) -> dict[str, int]:
    """Re-read up to ``limit`` series rosters, spending at most ``member_calls``
    arts calls on new members overall. Returns counts."""
    stats = {"series": 0, "complete": 0, "members_linked": 0}
    if limit <= 0:
        return stats
    rows = (
        session.query(Series.id, Series.name, Series.url, Series.book_count)
        .filter(Series.url.isnot(None), Series.url != "")
        .order_by(
            Series.roster_synced_at.isnot(None),  # NULL (never read / failed) first
            Series.roster_synced_at,
            Series.book_count.desc(),
        )
        .limit(limit)
        .all()
    )
    log.info("Roster refresh: %d series (budget %d, %d member calls)", len(rows), limit, member_calls)
    budget = MemberBudget(member_calls)
    for series_id, name, url, book_count in rows:
        if budget.remaining <= 0:
            log.info("Roster refresh: member-call budget exhausted after %d series", stats["series"])
            break
        stats["series"] += 1
        try:
            linked = ingest_series(
                session, client, series_id, name, url, book_count or 0,
                budget=budget, commit_each=True,
            )
            stats["members_linked"] += linked
            if session.get(Series, series_id).roster_synced_at is not None:
                stats["complete"] += 1
            _heartbeat(session, run_id)
            commit_with_retry(session, context=f"roster refresh series {series_id}")
        except Exception:
            session.rollback()
            log.exception("Roster refresh: series_id=%d failed", series_id)
    log.info(
        "Roster refresh done: %d series, %d complete, %d members newly linked",
        stats["series"], stats["complete"], stats["members_linked"],
    )
    return stats


def refresh_stale_books(
    session: Session,
    client: RateLimitedClient,
    *,
    limit: int,
    older_than: timedelta = STALE_BOOK_AGE,
    run_id: int | None = None,
) -> dict[str, int]:
    """Re-check up to ``limit`` books not refreshed for ``older_than``.

    A 404 from the arts API means the book is gone from LitRes: it is marked
    not available with subscription (rows are kept: listened/ignored lists
    reference them). Other errors leave the book for the next night.
    """
    stats = {"checked": 0, "sub_on": 0, "sub_off": 0, "gone": 0, "failed": 0}
    if limit <= 0:
        return stats
    cutoff = datetime.now(timezone.utc) - older_than
    rows = (
        session.query(Book.id)
        .filter(or_(Book.cached_at.is_(None), Book.cached_at < cutoff))
        .order_by(Book.cached_at)
        .limit(limit)
        .all()
    )
    log.info("Stale-book refresh: %d books older than %s (budget %d)", len(rows), cutoff.date(), limit)
    for i, (book_id,) in enumerate(rows, 1):
        book = session.get(Book, book_id)
        before = book.is_available_with_subscription
        try:
            detail = fetch_arts_detail(client, book_id)
            refresh_known_book(book, detail.art)
            if detail.release_date is not None and book.release_date is None:
                book.release_date = detail.release_date
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 410):
                stats["failed"] += 1
                log.warning("Stale-book refresh: book %d — HTTP %d", book_id, exc.response.status_code)
                continue
            book.is_available_with_subscription = False
            book.cached_at = datetime.now(timezone.utc)
            stats["gone"] += 1
            log.info("Stale-book refresh: book %d gone from LitRes (HTTP %d)", book_id, exc.response.status_code)
        except Exception as exc:
            stats["failed"] += 1
            log.warning("Stale-book refresh: book %d — %s", book_id, exc)
            continue
        stats["checked"] += 1
        after = book.is_available_with_subscription
        if after and not before:
            stats["sub_on"] += 1
        elif before and not after:
            stats["sub_off"] += 1
        if i % 50 == 0:
            _heartbeat(session, run_id)
            commit_with_retry(session, context="stale-book refresh")
    _heartbeat(session, run_id)
    commit_with_retry(session, context="stale-book refresh")
    log.info(
        "Stale-book refresh done: %d checked, %d joined subscription, %d left it, %d gone, %d failed",
        stats["checked"], stats["sub_on"], stats["sub_off"], stats["gone"], stats["failed"],
    )
    return stats
