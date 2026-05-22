#!/usr/bin/env bash
# Rebuild sympy-lean4-sandbox:latest on arcbox and update the local tag.
# Usage: ./build.sh [--push]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="sympy-lean4-sandbox:latest"

echo "==> Building $IMAGE on arcbox (DOCKER_HOST=ssh://freazer@arcbox)"
DOCKER_HOST=ssh://freazer@arcbox docker build \
    --progress=plain \
    -t "$IMAGE" \
    "$SCRIPT_DIR"

echo "==> Build complete. Image is live on arcbox as $IMAGE"
echo "==> Maestro sandbox will use it immediately (no restart needed — docker run pulls fresh on next call)."
