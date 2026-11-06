#!/usr/bin/env bash
# Run the LitRes Advanced Filters app container (image must be built first: ./bin/build_docker.sh).
# Usage: from project root, ./bin/run_docker.sh
# From the host (Windows): open http://localhost:5000 in your browser.
# From inside the devcontainer: open http://host.docker.internal:5000 (127.0.0.1 is the container, not the host).
# Override: IMAGE_NAME=... CONTAINER_NAME=... PORT=... ./bin/run_docker.sh
# persistent/ is mounted for DB/config. App runs as UID 10001; if write fails: sudo chown -R 10001:10001 persistent

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-litres-advanced-filters-app}"
CONTAINER_NAME="${CONTAINER_NAME:-litres-advanced-filters-app}"
PORT="${PORT:-5000}"

cd "$REPO_ROOT"

echo "Running container (port $PORT, persistent/ mounted)"
echo "  Image: $IMAGE_NAME"
echo "  Container name: $CONTAINER_NAME"
echo "  On host machine: http://localhost:$PORT  or  http://127.0.0.1:$PORT"
echo "  From devcontainer: http://host.docker.internal:$PORT"
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
ENV_ARGS=()
# The image refuses to start without SECRET_KEY (the built-in dev key would
# make session cookies forgeable). For a local run without one, use a random
# key: sessions then reset whenever the container is recreated.
if [ -z "$SECRET_KEY" ]; then
  echo "  SECRET_KEY not set — using a random key for this run (logins reset on restart)"
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
ENV_ARGS+=(-e "SECRET_KEY=$SECRET_KEY")
[ -n "$LITRES_EMAIL" ] && ENV_ARGS+=(-e "LITRES_EMAIL=$LITRES_EMAIL")
[ -n "$LITRES_PASSWORD" ] && ENV_ARGS+=(-e "LITRES_PASSWORD=$LITRES_PASSWORD")
[ -n "$ADMIN_EMAILS" ] && ENV_ARGS+=(-e "ADMIN_EMAILS=$ADMIN_EMAILS")
# Security settings (see app/config/config.py): cookie Secure flag mode,
# extra origins for the CSRF check, trusted reverse-proxy hops for client IPs
[ -n "$SESSION_COOKIE_SECURE" ] && ENV_ARGS+=(-e "SESSION_COOKIE_SECURE=$SESSION_COOKIE_SECURE")
[ -n "$TRUSTED_ORIGINS" ] && ENV_ARGS+=(-e "TRUSTED_ORIGINS=$TRUSTED_ORIGINS")
[ -n "$TRUSTED_PROXY_HOPS" ] && ENV_ARGS+=(-e "TRUSTED_PROXY_HOPS=$TRUSTED_PROXY_HOPS")

docker run --rm --name "$CONTAINER_NAME" -p "127.0.0.1:$PORT:5000" -v "$REPO_ROOT/persistent:/app/persistent" "${ENV_ARGS[@]}" "$IMAGE_NAME"
