# Detailed Development Plan v2

> Updated: 2026-03-05. Phase table links to per-phase plan files in `docs/design/`. Load only the In-Progress phase file — done and to-do phases are not needed in context.

---

## Phase index

| Phase | Status | Comment | Plan file |
|-------|--------|---------|-----------|
| Phase 0 | Done | V1 PoC — on-demand scraping prototype, replaced by v2 | [design/Phase-0-plan.md](design/Phase-0-plan.md) |
| Phase 1 | Done | Architecture pivot: v2 plan fully refined, docs restructured | [design/Phase-1-plan.md](design/Phase-1-plan.md) |
| Phase 2 | Done | DB schema v2: sync_config + sync_run tables, drop V1 artifacts, add indexes | [design/Phase-2-plan.md](design/Phase-2-plan.md) |
| Phase 3 | Done | Research spikes: all LitRes API questions resolved | [design/Phase-3-plan.md](design/Phase-3-plan.md) |
| Phase 4 | Done | Bulk sync engine: full catalog crawl, series ingest, integration tests, first real sync | [design/Phase-4-plan.md](design/Phase-4-plan.md) |
| Phase 5 | Done | Delta sync engine: newest-first crawl, stop condition, CLI + Flask endpoint, tests | [design/Phase-5-plan.md](design/Phase-5-plan.md) |
| Phase 6 | Done | BE service layer: CatalogService, all filters + sorts, series grouping, 39 unit tests | [design/Phase-6-plan.md](design/Phase-6-plan.md) |
| Phase 7 | Done | UI Design: "Warm Library at Dusk" — layout, colors, typography, components, a11y, responsive | [design/Phase-7-plan.md](design/Phase-7-plan.md) |
| Phase 8 | Done | UI Implementation: design tokens → layout → components → a11y → responsive (5 sub-phases) | [design/Phase-8-plan.md](design/Phase-8-plan.md) |
| Phase 9 | Done | Ignore list (F-9): "В игнор" button, route, optimistic UI hide | [design/Phase-9-plan.md](design/Phase-9-plan.md) |
| Phase 10 | Done | Listened tracking + profile sync (v1.1): F-5 hide listened, F-11 incomplete series | [design/Phase-10-plan.md](design/Phase-10-plan.md) |
| Phase 11 | Done | Multi-user auth: per-user sessions, per-visitor user creation, profile sync pagination fix | [design/Phase-11-plan.md](design/Phase-11-plan.md) |
| Phase 12 | Done | Code cleanup & refactor: SQL pagination, delta removal, README rewrite | [design/Phase-12-plan.md](design/Phase-12-plan.md) |
| Phase 13 | Done | GitHub Actions CI/CD: Docker Hub publish, semver, README badges | [design/Phase-13-plan.md](design/Phase-13-plan.md) |
| Phase 14 | Done | Admin panel: sync monitoring, cron management, failed-book retry | [design/Phase-14-plan.md](design/Phase-14-plan.md) |
| Phase 15 | Done | Polish: cron PATH fix, sync type distinction, series URL filters, persistent sessions, background profile sync | [design/Phase-15-plan.md](design/Phase-15-plan.md) |

---

## Architectural decision

The v1 approach (on-demand scraping during page load) is replaced:

| | Old | New |
|--|-----|-----|
| **Serving** | Flask request → scrape LitRes → cache in DB → serve | Flask reads DB only (pure SQL, no HTTP in request path) |
| **Data source** | Per-request LitRes scrape | Independent sync process fills DB upfront |
| **Filtering** | Python-level post-processing | SQL `WHERE`/`HAVING` clauses |
| **Sorting** | Not possible without full dataset | SQL `ORDER BY` on any indexed column |

**Why:** Per-page wait time is unacceptable UX. Sorting requires the full local dataset. Two-stage filter model (LitRes-native + custom) was complex and brittle.

**SQLite stays:** 300–500 k rows is well within SQLite's capabilities for read-heavy single-writer workloads.

---

## Sync strategy

Three independent sync processes. Each creates its own `sync_run` row and can be triggered separately via CLI or Flask admin endpoint.

**Bulk sync — weekly:** Full re-scan of all pages of Лёгкое чтение (`legkoe-chtenie`, id=201583) with fixed filters: audiobook, ru, subscription=True. New books → full ingest via `GET /arts/{id}` (genres, series, persons). Known books → update ratings only. Series pages fetched without subscription filter (needed for F-10 correctness).

**Incremental sync:** Use `bulk --max-pages 30` to fetch newest pages, update known books' ratings, and ingest new books. Same resume/time-box support as full bulk.

**Profile sync — on demand (v1.1, Phase 9):** Authenticate → fetch "Прослушанные" page → upsert `user_listened_book`. Unauthenticated for bulk to protect the subscription account.

### CLI and Flask endpoint reference

| Command | CLI | Flask endpoint |
|---------|-----|----------------|
| Genre tree | `python -m app.sync genres` | — |
| Bulk sync | `python -m app.sync bulk [--resume] [--max-pages N] [--dry-run] [--verbose]` | `POST /admin/sync/bulk` |
| Retry failed | `python -m app.sync retry-failed [--failed-file PATH]` | — |
| Profile sync | `python -m app.sync profile [--dry-run]` | `POST /admin/sync/profile` |
| Status | — | `GET /admin/sync/status` |

**Log files** (`persistent/sync/`, survives container restarts):
- `sync.log` — rotating full log; per-book detail always present regardless of `--verbose`
- `failed_books.jsonl` — one JSON object per failed book: `{book_id, title, url, page_number, error, traceback, raw_card}`

---

## Filters and sorting specification

> Established 2026-03-03. Supersedes F-1 through F-4b (LitRes-native filters) — baked into sync scope; not exposed in UI.

### User filters

| Filter | Default | Notes |
|--------|---------|-------|
| **Genre** | None (show all) | Sub-genre within Лёгкое чтение, up to 3 levels |
| **Series only** | Off | Mutually exclusive with Standalones only |
| ↳ Series size | min=2, max=-- | Active only when Series only is on; max is optional |
| ↳ Full series under subscription | Off | F-10; active only when Series only is on |
| **Standalones only** | Off | Mutually exclusive with Series only; both off = show all |
| **Exclude authors** | Off | User-editable list of author name strings to exclude |
| **Exclude narrators** | Off | Default list: "Литрес Авточтец". User-editable. F-6. |
| **Rating** | Off (min=3, max=--) | When enabled: min defaults to 3, max is optional. F-8. |
| **Hide listened** | Off | v1.1 only; requires profile sync. F-5. |
| **Only incomplete series** | Off | v1.1 only; requires profile sync. F-11. |

**UI constraint:** Series only and Standalones only are mutually exclusive — enabling one must automatically disable the other.

### Sorting options

| Sort | Default | SQL | Notes |
|------|---------|-----|-------|
| По новизне | ✓ | `release_date DESC` | `last_released_at` from catalog response → `Book.release_date` |
| По рейтингу | | `rating_avg DESC` | |
| По количеству голосов | | `rating_count DESC` | |
| По количеству комментариев | | `rating_count DESC` | LitRes requires star rating to comment → `reviews_count ≈ rating_count`; same column as "По голосам" |

---

## Resolved open questions

| Question | Decision |
|----------|----------|
| Sync scope | Single crawl of Лёгкое чтение (`legkoe-chtenie`, id=201583): audiobook, ru, subscription=True |
| Series enrichment timing | Inline: when a new series is encountered, immediately fetch the series page |
| Series page subscription filter | NOT applied — fetch all series books to support F-10 correctly |
| Genre-per-book mapping | Call `GET /arts/{id}` per new book; `genres[]` returns all genre IDs |
| Catalog URL/params | `GET https://api.litres.ru/foundation/api/genres/201583/arts/facets` with `art_types=audiobook&languages=ru&only_litres_subscription_arts=true&limit=24&offset=N*24&o=new` |
| Narrator data source | Present in catalog card `persons[]` with `role="reader"` — no extra call needed |
| Book release date for sort | `last_released_at` in every catalog response → `Book.release_date` (DateTime, indexed) |
| Comment count for sort | `reviews_count ≈ rating_count` → "По комментариям" uses `rating_count DESC`; no separate column |
| LitRes-native filter UI (F-1 to F-4b) | Removed from UI — baked into sync scope |
| Author exclusion filter | Stored in `user_settings.excluded_authors_json`; same pattern as narrator exclusion |
| Series only / Standalones only | Mutually exclusive; both off = show all |
| Rating filter defaults | min=3, max=-- (optional); disabled by default |
| Series size defaults | min=2, max=-- (optional); shown only when Series only is on |
| Sync trigger | CLI primary; Flask admin endpoints mirror all CLI args as JSON body |
| Weekly re-sync | Re-run bulk; known books get ratings updated (~99% of the run) |
| Auth for listened sync | Profile-only scrape (not embedded in bulk) to protect subscription account |
| BE/FE split | Phase 6 = service layer (unit tested); Phase 7 = UI (maps to service) |
| Series+standalone result grouping | Python grouping after SQL fetch (LEFT JOIN book_series), not SQL UNION |
| First-book cover for series card | `MIN(position_in_series)`; fallback `MIN(book.id)` |
