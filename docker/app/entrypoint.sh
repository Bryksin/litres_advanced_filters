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
else
    echo "  First boot: copying default crontab to /app/persistent/crontab"
    cp /app/crontab /app/persistent/crontab
    chown appuser:appuser /app/persistent/crontab
fi

# W14: cron starts jobs with an empty environment. The container's settings
# are written as VAR=value lines to an appuser-only header file outside the
# volume (no secrets on persistent/) and prepended when the crontab is
# installed. The admin cron editor must install "$CRON_ENV_FILE + crontab"
# too, or edited jobs lose the header until the next restart. Values with a
# newline or '%' are skipped: crontab cannot express them safely.
CRON_ENV_FILE=/app/.cron-env
umask 077
{
    for v in DATABASE_URI SECRET_KEY LITRES_EMAIL LITRES_PASSWORD ADMIN_EMAILS SYNC_LOG_RETENTION_DAYS; do
        eval "val=\${$v-}"
        [ -z "$val" ] && continue
        case "$val" in
            *%*|*"
"*) echo "  Skipping $v in cron env (unsupported character)" >&2; continue ;;
        esac
        printf '%s=%s\n' "$v" "$val"
    done
} > "$CRON_ENV_FILE"
umask 022
chown appuser:appuser "$CRON_ENV_FILE"
cat "$CRON_ENV_FILE" /app/persistent/crontab | crontab -u appuser -

echo "Applying database migrations..."
gosu appuser python -m alembic upgrade head

# No sync can be alive before cron and gunicorn start: mark leftover 'running'
# sync_run rows failed (they would block that night's cron sync) and apply log
# retention (old per-run logs, cron.log cap). Non-fatal.
echo "Boot cleanup (stale sync runs, log retention)..."
gosu appuser python -m app.sync boot-cleanup || echo "  boot-cleanup failed (ignored)"

# Start cron only after migrations + cleanup so a midnight job never races them.
cron

echo "Starting app server..."
exec gosu appuser "$@"
