#!/usr/bin/env bash
# scripts/deploy.sh
# Run on the VPS to pull and redeploy all services.
# In CI, set DOPPLER_TOKEN in the environment (e.g. from GitHub Actions secret).
# Uses the 'stg' Doppler config. Switch to 'prd' when live money is enabled.
set -euo pipefail

echo "=== WINS deploy ==="

# Capture current HEAD so rollback.sh can return to it if needed
git rev-parse HEAD > .deploy_sha
echo "--- Pre-deploy commit: $(cat .deploy_sha) ---"

git pull origin main

echo "--- Syncing secrets from Doppler ---"
# DOPPLER_TOKEN is used automatically by the CLI when set in the environment.
# On dev machines it falls back to the scoped personal login (doppler.yaml).
doppler secrets download --no-file --format env --config stg > .env.doppler

docker compose build
docker compose up -d

echo "--- Running DB migrations ---"
# Pipe the freshly-pulled host file via stdin rather than reading the container's
# copy. init.sql is a single-file bind mount, and replacing the file on the host
# (git pull) does not update the inode the long-lived wins-db container sees — so
# `-f /docker-entrypoint-initdb.d/init.sql` would run a stale schema. stdin always
# uses the current host file.
docker compose exec -T wins-db psql -U wins -d wins -v ON_ERROR_STOP=1 < wins/db/init.sql
echo "✓ Migrations complete"

docker compose ps
echo "=== Deploy complete ==="
