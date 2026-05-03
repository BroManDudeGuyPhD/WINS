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
docker compose exec -T wins-db psql -U wins -d wins -f - <<'SQL'
CREATE TABLE IF NOT EXISTS social_history (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    token            VARCHAR(20) NOT NULL,
    date             DATE NOT NULL,
    social_dominance DOUBLE PRECISION,
    interactions_24h DOUBLE PRECISION,
    sentiment        DOUBLE PRECISION,
    galaxy_score     DOUBLE PRECISION,
    alt_rank         INTEGER,
    price_open       DOUBLE PRECISION,
    price_close      DOUBLE PRECISION,
    price_high       DOUBLE PRECISION,
    price_low        DOUBLE PRECISION,
    volume_24h       DOUBLE PRECISION,
    UNIQUE (token, date)
);
CREATE INDEX IF NOT EXISTS idx_social_history_token_date ON social_history (token, date DESC);
SELECT COUNT(*) AS existing_rows FROM social_history;
SQL
echo "✓ Migrations complete"

docker compose ps
echo "=== Deploy complete ==="
