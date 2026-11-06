"""CLI entry point for sync commands.

Usage:
    python -m app.sync genres [--verbose]
    python -m app.sync bulk [--resume] [--max-pages N] [--max-hours H] [--dry-run] [--verbose]
    python -m app.sync heal [--dry-run] [--verbose]
    python -m app.sync retry-failed [--failed-file PATH]
    python -m app.sync boot-cleanup   # container start: reap 'running' rows, prune logs
    python -m app.sync prune-logs

Every command first prunes old per-run log files and caps cron.log (S15).
"""

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.sync",
        description="LitRes catalog sync commands",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # genres
    genres_cmd = sub.add_parser("genres", help="Fetch LitRes genre tree and upsert into DB")
    genres_cmd.add_argument("--verbose", action="store_true", help="Print detailed log lines to console")

    # bulk
    bulk = sub.add_parser("bulk", help="Run full catalog bulk sync")
    bulk.add_argument(
        "--resume",
        action="store_true",
        help="Resume the last interrupted sync run from last_page_fetched",
    )
    bulk.add_argument("--max-pages", type=int, metavar="N", help="Stop after N pages")
    bulk.add_argument(
        "--max-hours",
        type=float,
        metavar="H",
        help="Stop after H hours (sets status=interrupted; resume later with --resume)",
    )
    bulk.add_argument("--dry-run", action="store_true", help="Fetch but do not write to DB")
    bulk.add_argument("--verbose", action="store_true", help="Print per-book log lines to console")
    bulk.add_argument(
        "--refresh-series", type=int, metavar="N", default=None,
        help="After a successful run, re-read up to N series rosters (default: nightly budget; 0 = off)",
    )
    bulk.add_argument(
        "--refresh-books", type=int, metavar="N", default=None,
        help="After a successful run, re-check up to N stale books (default: nightly budget; 0 = off)",
    )
    bulk.add_argument(
        "--no-refresh-genres", action="store_true",
        help="Do not re-scrape the genre tree before a full (no --max-pages) run",
    )

    # retry-failed
    retry = sub.add_parser("retry-failed", help="Retry books from failed_books.jsonl")
    retry.add_argument(
        "--failed-file",
        metavar="PATH",
        help="Path to failed_books.jsonl (default: persistent/sync/failed_books.jsonl)",
    )

    # heal
    heal_cmd = sub.add_parser("heal", help="Fix structural gaps (missing narrators, genres, authors)")
    heal_cmd.add_argument("--dry-run", action="store_true", help="Report without writing")
    heal_cmd.add_argument("--verbose", action="store_true", help="Print detailed log lines to console")

    # profile
    profile_cmd = sub.add_parser("profile", help="Sync listened books from LitRes profile")
    profile_cmd.add_argument("--user-id", type=int, required=True,
        help="Local user ID to sync listened books for")
    profile_cmd.add_argument("--dry-run", action="store_true", help="Fetch but do not write to DB")
    profile_cmd.add_argument("--verbose", action="store_true", help="Print detailed log lines to console")

    sub.add_parser("boot-cleanup", help="Container start: mark leftover 'running' syncs failed, prune logs")
    sub.add_parser("prune-logs", help="Delete old sync log files and cap cron.log")

    return parser


def _prune_logs() -> None:
    from app.config.config import Config
    from app.sync.logging_setup import prune_sync_logs

    try:
        stats = prune_sync_logs(Config.SYNC_DIR)
        if stats["deleted"] or stats["cron_log_truncated"]:
            print(
                f"Log retention: deleted {stats['deleted']} file(s), "
                f"cron.log truncated={bool(stats['cron_log_truncated'])}, "
                f"freed {stats['freed_bytes'] / 1e6:.1f} MB"
            )
    except Exception as exc:  # retention must never block a sync
        print(f"Log retention failed (ignored): {exc}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    _prune_logs()

    if args.command == "prune-logs":
        return

    if args.command == "boot-cleanup":
        from app.db import SessionLocal
        from app.sync.common import reap_all_running_sync_runs

        with SessionLocal() as session:
            n = reap_all_running_sync_runs(session)
        print(f"Boot cleanup: {n} leftover running sync_run row(s) marked failed")
        return

    if args.command == "bulk":
        from app.sync.refresh import DEFAULT_BOOK_REFRESH, DEFAULT_SERIES_REFRESH
        if args.refresh_series is None:
            args.refresh_series = DEFAULT_SERIES_REFRESH
        if args.refresh_books is None:
            args.refresh_books = DEFAULT_BOOK_REFRESH

    if args.command == "genres":
        from app.sync.genres import run_genres
        run_genres(verbose=args.verbose)

    elif args.command == "bulk":
        from app.sync.bulk import run_bulk_cli
        run_bulk_cli(args)

    elif args.command == "retry-failed":
        from app.sync.bulk import run_retry_failed_cli
        run_retry_failed_cli(args)

    elif args.command == "heal":
        from app.sync.logging_setup import setup_sync_logging
        from app.sync.common import make_run_tag
        from app.config.config import Config
        run_tag = make_run_tag()
        setup_sync_logging(Config.SYNC_DIR, run_tag, verbose=args.verbose)

        from app.db import SessionLocal
        from app.scrapers.client import RateLimitedClient
        from app.sync.heal import heal_authors, heal_genres, heal_narrators

        with SessionLocal() as session:
            with RateLimitedClient() as client:
                n_stats = heal_narrators(session, client, dry_run=args.dry_run)
                g_stats = heal_genres(session, client, dry_run=args.dry_run)
                a_stats = heal_authors(session, client, dry_run=args.dry_run)
                if not args.dry_run:
                    session.commit()

        print(
            f"Heal narrators: {n_stats['total']} total, {n_stats['healed']} healed, "
            f"{n_stats['unresolvable']} unresolvable, {n_stats['failed']} failed"
        )
        print(
            f"Heal genres: {g_stats['total']} total, {g_stats['healed']} healed, "
            f"{g_stats['still_empty']} still empty, {g_stats['failed']} failed"
        )
        print(
            f"Heal authors: {a_stats['total']} total, {a_stats['healed']} healed, "
            f"{a_stats['unresolvable']} unresolvable, {a_stats['failed']} failed"
        )

    elif args.command == "profile":
        from app.config.config import Config
        from app.db import SessionLocal
        from app.sync.profile import run_profile

        email = Config.LITRES_EMAIL
        password = Config.LITRES_PASSWORD

        if not email:
            print("Error: LITRES_EMAIL environment variable must be set.")
            sys.exit(1)

        run_profile(
            session_factory=SessionLocal,
            email=email,
            password=password,
            user_id=args.user_id,
            dry_run=args.dry_run,
        )

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
