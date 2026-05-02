# Current Development State

Lightweight index of all phases. Read this table first; then read the linked file **only for In-Progress phases**. Done phases are complete — no need to load their details.

| Phase | Status | Comment | Detailed file |
|-------|--------|---------|---------------|
| Phase 0 | Done | Project V1 initial PoC — on-demand scraping prototype | [archive/Current Development State.md](archive/Current Development State.md) |
| Phase 1 | Done | Architecture pivot: v2 plan fully refined, docs workflow restructured | [Detailed Development Plan v2.md](Detailed Development Plan v2.md) |
| Phase 2 | Done | DB schema v2: new sync tables, drop V1 on-demand artifacts, add indexes | [status/Phase-2-status.md](status/Phase-2-status.md) |
| Phase 3 | Done | Research spikes: all API questions resolved; release_date added to 0001 (no 0002); reviews_count dropped | [status/Phase-3-status.md](status/Phase-3-status.md) |
| Phase 4 | Done | Bulk sync engine complete. All bugs fixed, integration tests pass (2 real pages), DB has real data. | [status/Phase-4-status.md](status/Phase-4-status.md) |
| Phase 5 | Done | Delta sync engine: `run_delta()`, CLI, Flask endpoint, shared `common.py`, 4 unit + 4 integration tests | [status/Phase-5-status.md](status/Phase-5-status.md) |
| Phase 6 | Done | BE service layer: CatalogService, SettingsService rewrite, genre service, controller rewire, template update, V1 cleanup. 39 unit tests. | [status/Phase-6-status.md](status/Phase-6-status.md) |
| Phase 7 | Done | UI Design: "Warm Library at Dusk" spec — 3-expert team, 17-section design doc | [status/Phase-7-status.md](status/Phase-7-status.md) |
| Phase 8 | Done | UI Implementation: all 5 sub-phases complete (tokens, layout, components, a11y, responsive) | [status/Phase-8-status.md](status/Phase-8-status.md) |
| Phase 9 | Done | Ignore list (F-9): 5 routes, optimistic UI, 7 unit tests, 72 total passing | [status/Phase-9-status.md](status/Phase-9-status.md) |
| Phase 9.1 | Done | Bug fixing & verification — 13 bugs fixed, 79 tests passing | [status/Phase-9.1-status.md](status/Phase-9.1-status.md) |
| Phase 9.2 | Done | Bug fixing round 2 — 8 bugs fixed, all features user-tested and confirmed working | [status/Phase-9.2-status.md](status/Phase-9.2-status.md) |
| Phase 10 | Done | Auth + profile sync + F-5 (hide listened) + F-11 (incomplete series) — 122 tests | [status/Phase-10-status.md](status/Phase-10-status.md) |
| Phase 11 | Done | Multi-user auth: per-user sessions, per-visitor user creation, profile sync pagination fix, 133 unit tests | [status/Phase-11-status.md](status/Phase-11-status.md) |
| Phase 12 | Done | Perf optimization (SQL pagination), delta removal, code cleanup, README rewrite — 125 tests | [design/Phase-12-plan.md](design/Phase-12-plan.md) |
| Phase 13 | Done | GitHub Actions CI/CD: PR checks, Docker Hub image publish, semver, README badges | [plans/2026-03-10-phase-13-cicd.md](plans/2026-03-10-phase-13-cicd.md) |

| Phase 14 | Done | Admin panel: sync monitoring, cron management, failed-book retry | [design/Phase-14-plan.md](design/Phase-14-plan.md) |

| Phase 15 | Done | Polish: cron fixes, sync type distinction, series links, session persistence, background profile sync — 158 tests | [design/Phase-15-plan.md](design/Phase-15-plan.md) |
