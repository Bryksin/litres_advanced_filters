# Phase 15 — Polish: cron fixes, sync types, series links, session persistence, background profile sync

**Status:** Done

---

## Background

Phase 14 shipped the admin panel with sync monitoring, cron management, and retry. After real-world use, several issues surfaced:

1. **Cron jobs silently failing** — `python: not found` because cron's minimal PATH doesn't include `/usr/local/bin`
2. **Stuck sync run** blocking new syncs — a crashed run left status=`running` indefinitely
3. **No distinction** between nightly delta and weekly full sync in the history table
4. **Series redirect** to LitRes opens mixed text+audio results (missing audiobook/subscription filters)
5. **Session expires** after a few days — user must re-login repeatedly
6. **Profile sync blocks login** — synchronous call makes login feel slow

---

## Tasks

### 15.1 — Fix crontab PATH for python discovery

**Problem:** Cron runs with `PATH=/usr/bin:/bin`. The container has `python3` at `/usr/local/bin/python3` but bare `python` is not on cron's PATH. Every cron job since April 4 has failed with `python: not found`.

**Fix:**
- Add `PATH=/usr/local/bin:/usr/bin:/bin` as the first line of `docker/app/crontab`
- This propagates to `persistent/crontab` on first boot; existing containers need the persistent copy updated too
- Also update admin panel's cron write logic (`admin_panel.py`) to preserve the PATH line when writing edited crontab

**Files:** `docker/app/crontab`, `app/controllers/admin_panel.py`

### 15.2 — Auto-recover stuck "running" sync runs

**Problem:** If the sync process crashes mid-run (e.g. `database is locked`), the SyncRun stays `status="running"` forever. `check_no_running_sync()` then blocks all future syncs.

**Fix:**
- In `check_no_running_sync()`: if a run has `status="running"` AND `finished_at IS NOT NULL`, auto-mark it as `failed` (it clearly crashed after setting finished_at)
- Additionally: if a run has `status="running"` AND `started_at` is older than 24 hours, auto-mark as `failed` with `error_message="Auto-recovered: sync exceeded 24h timeout"`
- Log a warning when auto-recovering

**Files:** `app/sync/common.py`

### 15.3 — Distinguish nightly vs. full sync type

**Problem:** Both nightly (100 pages) and full weekly syncs record `type="bulk"` in the SyncRun table. The admin history table shows all as "bulk" with no way to tell which is which.

**Fix:**
- Change the `type` field to use `"delta"` when `max_pages` is set, and `"bulk"` when no limit (full sync)
- Update `_open_sync_run()` or `run_bulk()` to accept/set the type based on `max_pages`
- Update admin dashboard template to show human-readable labels: "Дельта" / "Полная"
- Update manual sync buttons: delta button should show "Дельта (100 стр.)", full should show "Полная синхронизация"

**Files:** `app/sync/bulk.py`, `templates/admin/dashboard.html`

### 15.4 — Series redirect with audiobook + subscription filters

**Problem:** Series card URL links to `https://www.litres.ru/series/slug/` which shows all formats (text + audio) and all purchase types. User expects audiobook-only, subscription-only view.

**Fix:**
- In `catalog_service.py`, append query parameters to series URLs: `?art_types=audiobook&lfrom=litres_subscription`
- Only apply to series URLs (not individual book URLs — those already land on the correct audiobook page)
- Use `urllib.parse.urlencode` for clean param construction

**Files:** `app/services/catalog_service.py`

### 15.5 — Persistent session (no expiry)

**Problem:** Flask's default session cookie expires when the browser session ends (or gets garbage-collected by the browser after a few days). User has to re-login frequently.

**Fix:**
- In `create_app()` or middleware: set `session.permanent = True` on every request
- Set `app.config["PERMANENT_SESSION_LIFETIME"]` to a long duration (e.g. 365 days)
- This makes the session cookie persistent across browser restarts

**Files:** `app/middleware.py`, `app/config/config.py`

### 15.6 — Background profile sync on activity (instead of blocking login)

**Problem:** Profile sync runs synchronously during login, blocking the UI. With permanent sessions, login happens rarely, so listened books go stale.

**Fix (two parts):**

**a) Make login non-blocking:**
- Move `run_profile()` call in `auth.py` `/login` to a background daemon thread (same pattern as manual sync trigger in admin_panel.py)
- Return login response immediately after authentication succeeds
- Profile sync runs in background; UI shows "Синхронизация прослушанного..." indicator

**b) Auto-trigger profile sync on activity:**
- In `_ensure_user()` middleware: if user is authenticated (has stored tokens), check when their last profile sync ran
- If last profile sync was >20 hours ago, spawn a background thread to run `run_profile()`
- Set a flag in session (e.g. `session["profile_sync_started_at"]`) to prevent re-triggering on every request within the same background run
- This ensures listened books stay fresh without requiring manual login

**Files:** `app/controllers/auth.py`, `app/middleware.py`

### 15.7 — Unit tests

- **Crontab PATH:** verify PATH line is present in default crontab; verify admin write preserves it
- **Stuck sync recovery:** test auto-recovery for runs with finished_at set, and for runs older than 24h
- **Sync type:** verify delta vs bulk type is set correctly based on max_pages
- **Series URL:** verify audiobook+subscription params appended to series URLs
- **Session persistence:** verify session.permanent is set
- **Background profile sync:** verify login returns immediately; verify middleware triggers sync when stale

---

## Definition of Done

- Cron jobs execute successfully in Docker (verify via `cron.log`)
- Stuck sync runs auto-recover; new syncs can start
- Admin history table distinguishes delta vs. full syncs
- Series cards open LitRes filtered to audiobooks + subscription
- Session persists across browser restarts (365 days)
- Profile sync is non-blocking on login
- Profile sync auto-triggers on activity if >20h stale
- All existing tests pass + new tests for each fix
- Alembic state unchanged (no DB migration needed — type field is already a string)

## Dependencies

- Phase 14 complete (Done)
- No new Python packages required
