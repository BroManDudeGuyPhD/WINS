#!/usr/bin/env bash
# scripts/deploy.sh
# Run on the Linux production server to pull and redeploy all services.
# In CI, set DOPPLER_TOKEN in the environment (e.g. from GitHub Actions secret).
set -euo pipefail

echo "=== WINS deploy ==="
git pull origin main

echo "--- Syncing secrets from Doppler ---"
# DOPPLER_TOKEN is used automatically by the CLI when set in the environment.
# On dev machines it falls back to the scoped personal login (doppler.yaml).
doppler secrets download --no-file --format env > .env.doppler

docker compose build
docker compose up -d
docker compose ps
echo "=== Deploy complete ==="
