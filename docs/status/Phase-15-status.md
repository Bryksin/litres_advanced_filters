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

## Deployment (2026-05-02)

- **Migration:** Mac mini (`mbp`) → Proxmox LXC (id=103, host `proxmox`, container `litres`)
- **Public URL:** https://litres.spidernest.duckdns.org/ (SSL via DuckDNS)
- **Verified post-deploy:**
  - Persistent session cookie sets `Expires=Sun, 02 May 2027` (365 days, working)
  - Stuck sync run id=39 manually marked as `failed`
  - New cron has PATH fix; first scheduled run: tonight 00:00 UTC

## Follow-up correction (2026-05-09, PR #8)

The 15.1 PATH fix did **not** stick on prod after redeploy. Investigation
on 2026-05-09 found `/app/persistent/sync/cron.log` full of `python: not found`
since 04-10 — last successful sync was 04-10 21:44.

Root cause: the entrypoint loads `/app/persistent/crontab` if it exists, and
that file was written by a pre-15.1 version of the admin panel (no `PATH=`
header). The persistent volume preserved it across every deploy, so the
fixed default `/app/crontab` was never used.

Resolved by PR #8 with three layers of defence:

1. Default crontab now invokes `/usr/local/bin/python` directly (absolute
   path; no PATH dependency).
2. Entrypoint heals stale persistent crontabs on every boot — injects `PATH=`
   if missing and rewrites bare `python -m` to absolute path. Idempotent.
3. `PATH=` line still kept in default and admin-write paths.

Profile sync was also failing (HTTP 404 from LitRes refresh endpoint, then
`LitresAuthError` because `LITRES_PASSWORD` is not set in the prod LXC env).
Re-login fallback is the workaround until the LitRes API change is
investigated; deployment story now requires `LITRES_EMAIL` + `LITRES_PASSWORD`
to be forwarded into the container.
