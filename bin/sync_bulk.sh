#!/bin/bash
# Run 8 hours of bulk sync (resume if interrupted), then stop.
# Schedule with cron: 0 8 * * * /path/to/bin/sync_bulk.sh >> /var/log/litres-sync.log 2>&1
#
# The script is idempotent:
#   - If sync is done: exits immediately (nothing to do)
#   - If sync is running: exits immediately (another process is active)
#   - Otherwise: starts/resumes for up to 8 hours

set -euo pipefail

CONTAINER_NAME="litres-advanced-filters-app"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting sync_bulk.sh"

docker exec "$CONTAINER_NAME" \
  poetry run python -m app.sync bulk \
    --resume \
    --max-hours 8

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] sync_bulk.sh finished"
