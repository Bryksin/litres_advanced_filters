# Phase 15 — Status

**Started:** 2026-05-02
**Completed:** 2026-05-02

## Task progress

| Task | Status | Notes |
|------|--------|-------|
| 15.1 Crontab PATH fix | Done | Added `PATH=/usr/local/bin:/usr/bin:/bin` to crontab; admin write preserves it |
| 15.2 Auto-recover stuck syncs | Done | Auto-recovers runs with finished_at set or >24h old |
| 15.3 Delta vs. full sync type | Done | `type="delta"` when max_pages set; Russian labels in dashboard |
| 15.4 Series URL filters | Done | `_series_url()` appends `art_types=audiobook&only_litres_subscription_arts=true` |
| 15.5 Persistent session | Done | `PERMANENT_SESSION_LIFETIME=365d`, `session.permanent=True` |
| 15.6 Background profile sync | Done | Login non-blocking; middleware auto-triggers if >20h stale |
| 15.7 Unit tests | Done | 15 new tests, 158 total passing |

## Decisions

- No DB migration needed — `SyncRun.type` is already a VARCHAR, changing values from "bulk" to "delta" is data-level
- Series URL params: `art_types=audiobook&only_litres_subscription_arts=true` (matches LitRes web UI filter)
- Stuck sync threshold: 24 hours
- Session lifetime: 365 days
- Profile sync staleness threshold: 20 hours (user requirement)

## Investigation findings (2026-05-02)

- SSH into `mbp` confirmed: cron daemon IS running (PID 12), but every job fails with `python: not found`
- `cron.log` has 29 lines of `/bin/sh: 1: python: not found`
- Last successful bulk sync: id=37 (2026-04-10), 100 pages delta
- Stuck run id=39: started via admin panel, crashed with `sqlite3.OperationalError: database is locked`, has `finished_at` set but status still `running`
- No bulk sync in 22 days
