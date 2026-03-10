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
[ -n "$SECRET_KEY" ] && ENV_ARGS+=(-e "SECRET_KEY=$SECRET_KEY")
[ -n "$LITRES_EMAIL" ] && ENV_ARGS+=(-e "LITRES_EMAIL=$LITRES_EMAIL")
[ -n "$LITRES_PASSWORD" ] && ENV_ARGS+=(-e "LITRES_PASSWORD=$LITRES_PASSWORD")

docker run --rm --name "$CONTAINER_NAME" -p "127.0.0.1:$PORT:5000" -v "$REPO_ROOT/persistent:/app/persistent" "${ENV_ARGS[@]}" "$IMAGE_NAME"
