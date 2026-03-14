#!/bin/sh
# Entrypoint: fix persistent volume ownership, start cron, then drop to appuser.
# Runs as root so chown and cron setup work; the app process itself always
# runs as non-root (appuser) via gosu.
set -e

echo "Fixing persistent/ ownership..."
chown -R appuser:appuser /app/persistent

echo "Installing sync crontab for appuser..."
crontab -u appuser /app/crontab
cron

echo "Applying database migrations..."
gosu appuser python -m alembic upgrade head

echo "Starting app server..."
exec gosu appuser "$@"
