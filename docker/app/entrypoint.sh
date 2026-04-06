#!/bin/sh
# Entrypoint: fix persistent volume ownership, start cron, then drop to appuser.
# Runs as root so chown and cron setup work; the app process itself always
# runs as non-root (appuser) via gosu.
set -e

echo "Fixing persistent/ ownership..."
chown -R appuser:appuser /app/persistent

echo "Installing sync crontab for appuser..."
if [ -f /app/persistent/crontab ]; then
    echo "  Using persistent crontab from /app/persistent/crontab"
    crontab -u appuser /app/persistent/crontab
else
    echo "  First boot: copying default crontab to /app/persistent/crontab"
    cp /app/crontab /app/persistent/crontab
    chown appuser:appuser /app/persistent/crontab
    crontab -u appuser /app/persistent/crontab
fi
cron

echo "Applying database migrations..."
gosu appuser python -m alembic upgrade head

echo "Starting app server..."
exec gosu appuser "$@"
