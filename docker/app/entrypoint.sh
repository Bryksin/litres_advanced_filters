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

    # Heal legacy persistent crontabs (idempotent migrations).
    # Older versions wrote crontabs without `PATH=` and with a bare `python`
    # invocation. Cron's environment doesn't include /usr/local/bin in PATH,
    # so those jobs fail with `python: not found`. Fix in place rather than
    # overwriting, to preserve any user-customised schedules.
    if ! grep -q '^PATH=' /app/persistent/crontab; then
        echo "  Migrating: injecting PATH= line"
        { echo "PATH=/usr/local/bin:/usr/bin:/bin"; echo ""; cat /app/persistent/crontab; } > /tmp/crontab.heal
        mv /tmp/crontab.heal /app/persistent/crontab
        chown appuser:appuser /app/persistent/crontab
    fi
    if grep -qE '&& python -m' /app/persistent/crontab; then
        echo "  Migrating: rewriting bare python to /usr/local/bin/python"
        sed -i 's|&& python -m|\&\& /usr/local/bin/python -m|g' /app/persistent/crontab
    fi

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
