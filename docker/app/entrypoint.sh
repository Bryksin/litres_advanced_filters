#!/bin/sh
# Auto-apply pending DB migrations before starting the app.
# Safe to run repeatedly — Alembic skips already-applied migrations.
set -e

echo "Applying database migrations..."
python -m alembic upgrade head

echo "Starting app server..."
exec "$@"
