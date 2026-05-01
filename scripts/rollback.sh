#!/usr/bin/env bash
# scripts/rollback.sh
# Reverts to the commit that was live before the last deploy.sh run.
# deploy.sh writes the pre-deploy SHA to .deploy_sha in the repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHA_FILE="$REPO_ROOT/.deploy_sha"

if [[ ! -f "$SHA_FILE" ]]; then
    echo "ERROR: $SHA_FILE not found — run deploy.sh at least once first." >&2
    exit 1
fi

TARGET=$(cat "$SHA_FILE")
echo "=== Rolling back to $TARGET ==="

git -C "$REPO_ROOT" checkout "$TARGET" -- .
docker compose -f "$REPO_ROOT/docker-compose.yml" build
docker compose -f "$REPO_ROOT/docker-compose.yml" up -d
docker compose -f "$REPO_ROOT/docker-compose.yml" ps

echo "=== Rollback complete — pinned to $TARGET ==="
echo "Fix the issue on main, then re-run deploy.sh."
