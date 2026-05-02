#!/usr/bin/env bash
# Build the LitRes Advanced Filters runtime Docker image.
# Usage: from project root, ./bin/build_docker.sh
# Override image name: IMAGE_NAME=myimage ./bin/build_docker.sh

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-litres-advanced-filters-app}"

cd "$REPO_ROOT"

echo "Building image: $IMAGE_NAME"
docker build -f docker/app/Dockerfile -t "$IMAGE_NAME" .

echo "Done. Run with: ./bin/run_docker.sh"
