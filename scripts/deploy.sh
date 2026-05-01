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
docker compose ps
echo "=== Deploy complete ==="
