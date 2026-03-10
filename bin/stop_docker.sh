#!/usr/bin/env bash
# Stop the LitRes Advanced Filters app container.
# Usage: from project root, ./bin/stop_docker.sh
# Override: CONTAINER_NAME=... ./bin/stop_docker.sh

set -e

CONTAINER_NAME="${CONTAINER_NAME:-litres-advanced-filters-app}"

if docker ps -q -f name="^${CONTAINER_NAME}$" | grep -q .; then
  echo "Stopping container: $CONTAINER_NAME"
  docker stop "$CONTAINER_NAME"
  echo "Stopped."
else
  echo "Container $CONTAINER_NAME is not running."
fi
