# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language conventions

- **Chat with user:** English (token-efficient; user may write in Russian or English — always respond in English)
- **Code, docstrings, comments, variable/class names, docs:** English
- **UI copy visible to the user (HTML labels, buttons, messages):** Russian (the site serves Russian-speaking users)

## Commands

```bash
# Dev server (from project root inside devcontainer)
poetry run flask --app app run

# Database migrations
./bin/db_migrate                        # Apply to head (default)
./bin/db_migrate current                # Show current revision
./bin/db_migrate history                # Show migration history
./bin/db_migrate downgrade -1          # Rollback one step
poetry run alembic revision --autogenerate -m "description"  # Generate new migration

# Docker (executes on host via mounted docker.sock)
./bin/build_docker.sh                   # Build image: litres-advanced-filters-app
./bin/run_docker.sh                     # Run (port 5000, mounts persistent/)
./bin/stop_docker.sh                    # Stop container

# Playwright (inside devcontainer, for API reverse engineering)
npm install                             # Once
npm run capture "https://www.litres.ru/genre/..."
npm run capture:pagination
```

## Architecture

**Two-stage filtering:**
1. Stage 1 — LitRes-native filters (genre, subscription, format, language, high rating) passed as query params to the LitRes facets API.
2. Stage 2 — Advanced filters (narrator exclusion, series length, rating range, ignore list, etc.) applied in Python after fetching results.

**Series grouping:** Results are grouped so one card represents an entire series. Series card title = series name, cover = first book's cover, rating = mean of all books, rating count = sum.

**Key data flow:** `app/scrapers/` → `app/__init__.py` (grouping logic) → `templates/` (Jinja2 render)

**Persistence:** All persistent data lives under `persistent/` (mounted as Docker volume):
- `persistent/db/litres_cache.db` — SQLite database (auto-created on first import of `app.db`)
- `persistent/covers/` — cached book covers (future)

## Project structure

```
app/
  __init__.py          # Flask routes, build_catalog_items() grouping
  config/config.py     # Paths, DATABASE_URI, SECRET_KEY (overridable via env vars)
  db/
    base.py            # SQLAlchemy engine, Base, SessionLocal (PRAGMA foreign_keys=ON)
    models.py          # 13 ORM models (SQLAlchemy 2.0 Mapped/mapped_column style)
    __init__.py        # Re-exports; importing this registers all models on Base.metadata
  scrapers/
    client.py          # RateLimitedClient (~1 req/s, browser-like headers)
    models.py          # Dataclasses: GenreNode, BookCard, BookDetails, SeriesPage, etc.
    genres.py          # fetch_genre_tree(), fetch_genre_tree_hierarchical()
    catalog.py         # fetch_catalog_page(), fetch_catalog() — facets API
    book.py            # fetch_book_page(), parse_book_page()
    series.py          # fetch_series_page(), parse_series_page()
    author.py          # fetch_author(), fetch_author_arts()
db/
  env.py               # Alembic env (imports app.db to register models)
  versions/            # Migration files
templates/
  base.html            # Master layout: dark sidebar + light main, blocks for content
  catalog.html         # Catalog page: genre tree accordion, filter sidebar, results grid
static/css/style.css   # Theme
docs/
  General Project Plan.md          # Requirements (F-1 to F-11), architecture decisions
  Detailed Development Plan.md     # Phase-by-phase tasks (Phase 0–15+)
  Current Development State.md     # Phase progress, standing decisions, next steps
  LitRes API Reverse Engineering.md # URLs, params, selectors, API endpoints
  db_schema.puml                    # PlantUML ERD (13 entities)
  samples/                          # Example API responses (JSON)
```

## Database schema

13 tables. Key relationships:
- `book_genre` — cache junction tracking freshness of book lists per genre
- `book_series` — with `position_in_series`; presence determines series vs. standalone card
- `subscription_per_book` — separate TTL cache for subscription status (F-10)
- `user_settings` — FK to `genre`; stores all filter state per user
- `user_ignored_book`, `user_listened_book` — per-user lists (F-9, F-5/F-11)

When adding columns or tables: edit `app/db/models.py`, run `alembic revision --autogenerate`, verify the generated file in `db/versions/`, then `./bin/db_migrate`.

## Git workflow

- **Never commit directly to master.** All changes go through feature branches + PRs.
- Branch naming: `feat/short-description`, `fix/short-description`, `docs/short-description`
- PRs are **squash-merged** to keep master history clean (one commit per feature/fix).
- CI must pass before merging.
- After merge, delete the feature branch.

## Current state

All phases complete (v1.0.2). DB live with all 13 tables. CI/CD via GitHub Actions. See `docs/Current Development State.md` for full phase history.

## Environment

- Python 3.14, Poetry, Flask 3.1, SQLAlchemy 2.0, Alembic 1.14, httpx, BeautifulSoup4
- Dev: Cursor devcontainer (`docker/dev/Dockerfile`), workspace at `/home/appuser/litres_advanced_filters`
- Production: Gunicorn 4 workers, `docker/app/Dockerfile`
- Rate limiting: ~1 req/s to litres.ru (configurable delay + jitter in `RateLimitedClient`)
