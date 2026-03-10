# LitRes Advanced Filters

**Advanced audiobook filtering and discovery for LitRes**
*Расширенные фильтры для поиска аудиокниг на ЛитРес*

[![CI](https://github.com/Bryksin/litres_advanced_filters/actions/workflows/ci.yml/badge.svg)](https://github.com/Bryksin/litres_advanced_filters/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/docker/v/briksins/litres_advanced_filters?label=Docker%20Hub&sort=semver)](https://hub.docker.com/r/briksins/litres_advanced_filters)

A self-hosted web app that extends LitRes audiobook filtering far beyond what the official site offers. Built for Russian-speaking audiobook listeners who browse the "Легкое чтение" genre and want better tools to find their next listen.

**Key capabilities:**

- **Series grouping** — one card per series with aggregated ratings, instead of dozens of individual book entries
- **Narrator exclusion** — filter out AI-narrated books ("Литрес Авточтец") or any narrator you dislike
- **Subscription-aware** — find complete series fully available under your LitRes subscription
- **Instant results** — pre-synced local database means zero API calls during page loads

### Disclaimer

This project is a personal tool created out of love for LitRes and its audiobook catalog. It is **not** affiliated with, endorsed by, or competing with [LitRes](https://www.litres.ru/) in any way. Every book card links directly to the original LitRes page — this app does not host, distribute, or sell any content.

The features here (series grouping, narrator filtering, advanced sorting) were submitted as feature requests to LitRes support on multiple occasions, but received no response. This project exists solely to fill that gap for personal use and to help fellow audiobook listeners discover books more easily on the official platform.

---

## Features

| Feature | Description | Requires login |
|---------|-------------|:--------------:|
| Genre filtering | Three-level genre hierarchy with sub-genre accordion | No |
| Series grouping | One card per series; cover = first book, rating = mean, rating count = sum | No |
| Sort options | Sort by release date, rating, or popularity | No |
| Series / standalones toggle | Show only series, only standalones, or both | No |
| Series size filter | Filter by min/max number of books in a series | No |
| Full series under subscription (F-10) | Show only series where every book is in the subscription catalog | No |
| Author exclusion | Exclude specific authors from results | No |
| Narrator exclusion (F-6) | Exclude specific narrators; "Литрес Авточтец" excluded by default | No |
| Rating range filter (F-8) | Set minimum and maximum average rating thresholds | No |
| Ignore list (F-9) | One-click hide for books/series you never want to see again | No |
| Hide listened books (F-5) | Hide books you have already finished on LitRes | Yes |
| Incomplete series finder (F-11) | Show only series where you have listened to some but not all books | Yes |
| Multi-user support | Anonymous sessions with per-user filter state and ignore lists | No |

F-5 and F-11 are mutually exclusive — enabling one disables the other.

---

## Architecture

```
                    Sync engine (offline)              Web app (online)
                    ────────────────────               ────────────────
LitRes API  ──────> Bulk sync crawls catalog  ──>  SQLite DB (~91k audiobooks, ~64 MB)
(api.litres.ru)     ~2 req/s, rate-limited             │
                                                       v
                                               Flask reads DB
                                               SQL-level filtering
                                               Series grouping in Python
                                               Jinja2 renders results
                                                       │
                                                       v
                                               Browser (no JS framework)
```

1. **Sync engine** crawls the LitRes catalog API and populates a local SQLite database with ~91,000 audiobooks, ~33,000 persons, ~7,000 series, and ~400 genres (~64 MB total).
2. **Flask web app** reads the local DB, applies SQL-level filters (genre, subscription, listened status) and Python-level post-filters (narrator exclusion, rating range, series grouping), then renders results via Jinja2 templates.
3. **Zero LitRes API calls during page loads** — all data is pre-synced, so response times are instant.
4. **Two sync modes:**
   - **Bulk** — full re-crawl of the entire catalog. First run takes ~12 hours at 2 req/s; subsequent runs update ratings and discover new books.
   - **Nightly incremental** — `bulk --max-pages N` fetches only the first N pages (sorted by newest), catching recent additions in seconds.

---

## Prerequisites

- **Python 3.14+** with [Poetry](https://python-poetry.org/), OR **Docker**
- ~100 MB free disk for the SQLite database
- Network access to `api.litres.ru` (for sync only, not required for serving the web app)
- (Optional) LitRes account credentials for F-5 / F-11 features

---

## Quick start

### Option A: From source

```bash
# Install dependencies
poetry install

# Apply database migrations
./bin/db_migrate

# Seed genre table (required before first sync)
poetry run python -m app.sync genres

# Run initial sync (limit to 10 pages for a quick test)
poetry run python -m app.sync bulk --max-pages 10

# Run the dev server
poetry run flask --app app run
```

### Option B: Docker

```bash
# Build the image locally
./bin/build_docker.sh

# Or pull a pre-built image from Docker Hub (no build needed)
docker pull briksins/litres_advanced_filters:latest
# Then set: export IMAGE_NAME=briksins/litres_advanced_filters

# Generate a secret key for session cookies
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")

# Run the container (port 5000, persistent/ mounted for DB storage)
./bin/run_docker.sh

# Open http://localhost:5000 in your browser
# Stop when done:
./bin/stop_docker.sh
```

After starting (either option), seed and sync the database:

```bash
# Inside Docker, prefix commands with: docker exec -it litres-advanced-filters-app ...
poetry run python -m app.sync genres
poetry run python -m app.sync bulk --max-pages 10
```

---

## Sync commands

The sync CLI lives at `app/sync/`. Run it with `python -m app.sync <subcommand>`.

### 1. Seed genres (required before bulk sync)

Fetches the full LitRes genre tree (~200-400 genres) and upserts into the `genre` table.
Must be run at least once before `bulk` — BookGenre FK references genre rows.

```bash
poetry run python -m app.sync genres
```

### 2. Bulk sync

Crawls all ~91k audiobooks in the target catalog. Fetches catalog pages + arts detail per new book + series pages for new series.

```bash
# Full run (will take ~12 hours for first sync at 2 req/s)
poetry run python -m app.sync bulk

# Limit to N pages (24 books/page)
poetry run python -m app.sync bulk --max-pages 10

# Stop after H hours, resume later (for daily cron)
poetry run python -m app.sync bulk --max-hours 8

# Resume last interrupted run from where it left off
poetry run python -m app.sync bulk --resume

# Resume with time limit (typical cron usage)
poetry run python -m app.sync bulk --resume --max-hours 8

# Dry run: fetch pages but do not write to DB
poetry run python -m app.sync bulk --dry-run --max-pages 2

# Verbose: print per-book log lines to console (in addition to log file)
poetry run python -m app.sync bulk --verbose --max-pages 5

# Combine options
poetry run python -m app.sync bulk --resume --max-hours 8 --verbose
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--resume` | Resume last interrupted sync from `last_page_fetched + 1` |
| `--max-pages N` | Stop after N catalog pages (each page = 24 books) |
| `--max-hours H` | Stop after H hours, set status=`interrupted` for cron resume |
| `--dry-run` | Fetch only — no DB writes. SyncRun still created for tracking |
| `--verbose` | Print DEBUG-level per-book lines to console (log file always has DEBUG) |

**Logs:** Written to `persistent/sync/sync.log` (rotating, 20 MB x 5 files).
**Failed books:** Written to `persistent/sync/failed_books.jsonl`.

### 3. Retry failed books

Retry books that failed during a bulk run (logged to `failed_books.jsonl`).

```bash
# Retry from default path (persistent/sync/failed_books.jsonl)
poetry run python -m app.sync retry-failed

# Retry from a specific file
poetry run python -m app.sync retry-failed --failed-file /path/to/failed_books.jsonl
```

Successfully retried books are removed from the file; still-failing ones remain.

### 4. Profile sync (listened books)

Syncs the authenticated user's finished books from LitRes. Required for F-5 (hide listened) and F-11 (incomplete series).

```bash
poetry run python -m app.sync profile --verbose
```

Requires `LITRES_EMAIL` and `LITRES_PASSWORD` environment variables.

### 5. Flask admin endpoints

The Flask app exposes admin endpoints for triggering sync from HTTP:

```bash
# Start a bulk sync in a background thread (returns 202 immediately)
curl -X POST http://localhost:5000/admin/sync/bulk \
     -H "Content-Type: application/json" \
     -d '{"max_pages": 5}'

# Resume last interrupted sync
curl -X POST http://localhost:5000/admin/sync/bulk \
     -H "Content-Type: application/json" \
     -d '{"resume": true, "max_hours": 8}'

# Dry run
curl -X POST http://localhost:5000/admin/sync/bulk \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true, "max_pages": 2}'

# Check latest sync status
curl http://localhost:5000/admin/sync/status
```

**POST `/admin/sync/bulk` body fields** (all optional):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `resume` | bool | false | Resume last interrupted run |
| `max_pages` | int | null | Stop after N pages |
| `max_hours` | float | null | Stop after H hours |
| `dry_run` | bool | false | Fetch only, no DB writes |

Returns `202 Accepted` with `{"status": "started", ...}` or `409 Conflict` if already running.

**GET `/admin/sync/status`** returns:
```json
{
  "id": 3,
  "type": "bulk",
  "status": "done",
  "pages_fetched": 3663,
  "books_upserted": 87912,
  "series_fetched": 4201,
  "started_at": "2026-03-04T08:00:00",
  "finished_at": "2026-03-05T08:00:00",
  "last_page_fetched": 3662,
  "error_message": null
}
```

### 6. Cron setup (automated daily sync)

```bash
# Make the wrapper executable (already done)
chmod +x bin/sync_bulk.sh

# Add to crontab (runs daily at 08:00, 8h window, resumes automatically)
crontab -e
# Add line:
0 8 * * * /home/user/litres_advanced_filters/bin/sync_bulk.sh >> /var/log/litres-sync.log 2>&1
```

See `docs/operations.md` for full details.

---

## Database migrations

```bash
./bin/db_migrate                        # Apply all pending migrations (default: upgrade head)
./bin/db_migrate current                # Show current revision
./bin/db_migrate history                # Show migration history
./bin/db_migrate downgrade -1           # Rollback one step

# Generate a new migration after editing app/db/models.py
poetry run alembic revision --autogenerate -m "describe your change"
# Then review db/versions/<new_file>.py before applying
```

---

## Configuration

### SECRET_KEY (required for production)

Flask signs session cookies with `SECRET_KEY`. The default dev value is fine for local development, but **must be overridden in production** — otherwise session cookies can be forged and users can be impersonated.

```bash
# Generate a secure key:
python -c "import secrets; print(secrets.token_hex(32))"

# Set via environment variable:
export SECRET_KEY="your-generated-key-here"

# Or pass to Docker (run_docker.sh forwards it automatically):
export SECRET_KEY="your-generated-key-here"
./bin/run_docker.sh
```

If the default dev key is detected on startup, a warning is logged:
```
WARNING app.start: SECRET_KEY is using the default dev value — set SECRET_KEY env var for production
```

### LitRes credentials (optional, for listened books sync)

Required only if you want F-5 (hide listened) and F-11 (incomplete series) features:

```bash
export LITRES_EMAIL="your@email.com"
export LITRES_PASSWORD="your-password"
```

---

## Docker

```bash
./bin/build_docker.sh                   # Build image: litres-advanced-filters-app
./bin/run_docker.sh                     # Run on port 5000 with persistent/ mounted
./bin/stop_docker.sh                    # Stop container
```

`run_docker.sh` automatically forwards `SECRET_KEY`, `LITRES_EMAIL`, and `LITRES_PASSWORD` environment variables to the container when set.

Inside the container the sync CLI and admin endpoints work the same way.

---

## Releasing a new version

This project follows [Semantic Versioning](https://semver.org/) and [Conventional Commits](https://www.conventionalcommits.org/).

| Commit prefix | Version bump | Example |
|---------------|-------------|---------|
| `feat:` | Minor (1.x.0) | New filter, new sync mode |
| `fix:`, `perf:`, `docs:` | Patch (1.0.x) | Bug fix, optimization, docs |
| `BREAKING CHANGE:` in body | Major (x.0.0) | DB migration, config change |

To release:

1. Update `version` in `pyproject.toml`
2. Commit: `git commit -am "release: v1.2.0"`
3. Tag: `git tag v1.2.0`
4. Push: `git push origin master --tags`

GitHub Actions will run tests, then build and publish the Docker image to Docker Hub.

---

## Running tests

### Unit tests (no network, fast)

Tests sync ingestion, services, controllers, middleware, and scraper logic with in-memory SQLite and mocked HTTP.

```bash
poetry run python -m pytest tests/unit/ -v
```

### Endpoint integration tests (no network)

Tests Flask admin endpoints (`/admin/sync/bulk`, `/admin/sync/status`) with in-memory DB.

```bash
poetry run python -m pytest tests/integration/test_bulk_sync_endpoints.py -v -m integration
```

### Bulk sync integration tests (hits real LitRes — ~7 min)

Runs `run_bulk()` against real LitRes API, limited to **2 pages** (48 books + series enrichment).
Requires network access to `api.litres.ru` and `www.litres.ru`.

```bash
poetry run python -m pytest tests/integration/test_bulk_sync.py -v -s -m integration
```

### All non-network tests (recommended for CI)

```bash
poetry run python -m pytest tests/unit/ tests/integration/test_bulk_sync_endpoints.py -v
```

### All tests including LitRes integration

```bash
poetry run python -m pytest tests/integration/ -v -s -m integration  # all integration
poetry run python -m pytest tests/ -v                                # all (unit + endpoint)
```

**Test counts:** 130+ tests total (unit + integration, no network required for most).

---

## Project structure

```
app/
  __init__.py          # Flask app package marker
  start.py             # create_app(), blueprint registration, bootstrap
  middleware.py        # Session middleware (anonymous user provisioning)
  config/config.py     # Paths, DATABASE_URI, rate limits, SYNC_DIR
  db/
    base.py            # SQLAlchemy engine, Base, SessionLocal
    models.py          # 13 ORM models (SQLAlchemy 2.0 Mapped/mapped_column style)
  models/
    catalog_query.py   # CatalogQuery dataclass for filter parameters
  scrapers/
    client.py          # RateLimitedClient (~2 req/s, browser-like headers)
    models.py          # Dataclasses: Art, ArtDetail, SeriesPage, etc.
    arts.py            # fetch_arts_detail() — GET /foundation/api/arts/{id}
    catalog.py         # fetch_catalog_page() — facets API
    series.py          # fetch_series_page() — HTML series pages
    genres.py          # fetch_genre_tree_hierarchical()
    auth.py            # litres_login(), litres_refresh(), get_valid_token()
    profile.py         # fetch_finished_book_ids() — user's listened books
  services/
    catalog_service.py # SQL query builder, filtering, pagination
    cards.py           # Series grouping, card construction
    genre_service.py   # Genre tree building for sidebar
    settings_service.py # User filter state persistence
  controllers/
    catalog.py         # GET / — catalog page with filters
    admin.py           # POST /admin/sync/bulk, GET /admin/sync/status
    auth.py            # POST /auth/login, POST /auth/logout, GET /auth/status
  sync/
    __main__.py        # CLI entry: python -m app.sync <subcommand>
    genres.py          # run_genres() — upserts genre tree
    ingest.py          # ingest_book(), ingest_series(), helpers
    bulk.py            # run_bulk(), run_retry_failed_cli()
    profile.py         # run_profile() — sync listened books from LitRes
    heal.py            # Fix narrator/genre gaps in existing data
    logging_setup.py   # Rotating file + console log handlers
bin/
  db_migrate           # Alembic wrapper
  sync_bulk.sh         # Cron wrapper: --resume --max-hours 8
  build_docker.sh      # Build production image
  run_docker.sh        # Run production container
  stop_docker.sh       # Stop container
templates/
  base.html            # Master layout: dark sidebar + light main area
  catalog.html         # Catalog page: genre accordion, filter sidebar, results grid
static/css/style.css   # Dark theme styles
persistent/            # Docker volume mount (gitignored)
  db/litres_cache.db   # SQLite database
  sync/sync.log        # Rotating sync log
  sync/failed_books.jsonl  # Failed book entries for retry
tests/
  unit/                # Fast unit tests (mocked, in-memory DB)
  integration/         # Integration tests (real LitRes or Flask test client)
  fixtures/            # Test data (genre JSON, etc.)
db/
  env.py               # Alembic env (imports app.db to register models)
  versions/            # Migration files
docs/
  Current Development State.md   # Phase table — read first each session
  Detailed Development Plan v2.md
  status/              # Per-phase status files
  design/              # Per-phase design plans
  operations.md        # Cron and ops instructions
  LitRes API Reverse Engineering.md
```

---

## Tech stack

- Python 3.14, Flask 3.1, Jinja2
- SQLAlchemy 2.0 (ORM), Alembic (migrations), SQLite
- httpx (HTTP client), BeautifulSoup4 (HTML parsing)
- Gunicorn (production), Poetry (dependency management)
- Docker (optional deployment)

---

*100% vibe-coded with [Claude Code](https://claude.ai/code) (Opus 4.6) by Anthropic. The human mass-approved permission prompts and mass-clicked "looks good" on code reviews.*
