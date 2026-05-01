#!/usr/bin/env bash
# scripts/setup.sh
# First-time project setup for WINS.
# Run from the repo root: bash scripts/setup.sh
set -euo pipefail

PYTHON=${PYTHON:-python3}
VENV_DIR=".venv"

echo ""
echo "=== WINS setup ==="
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

check() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: '$1' not found. $2"
        exit 1
    fi
}

check docker   "Install Docker Desktop: https://docs.docker.com/get-docker/"
check doppler  "Install Doppler CLI: https://docs.doppler.com/docs/install-cli"
check "$PYTHON" "Install Python 3.12+: https://python.org"

echo "✓ Prerequisites found"

# ── 2. Python venv ────────────────────────────────────────────────────────────

if [ ! -d "$VENV_DIR" ]; then
    echo "--- Creating Python virtual environment ---"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Activate — works on bash (Linux/Mac/Git Bash on Windows)
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate" 2>/dev/null || source "$VENV_DIR/Scripts/activate"

echo "--- Installing Python dependencies ---"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "✓ Python deps installed"

# ── 3. Doppler ────────────────────────────────────────────────────────────────

if [ ! -f "doppler.yaml" ]; then
    echo "ERROR: doppler.yaml not found. Run this script from the repo root."
    exit 1
fi

# Check if this project is already configured for the current user
if ! doppler secrets get ANTHROPIC_API_KEY &>/dev/null 2>&1; then
    echo ""
    echo "--- Doppler login required ---"
    echo "This links your Doppler account to this project directory."
    echo "It will NOT affect other Doppler projects on this machine."
    echo ""
    doppler setup
fi

echo "--- Exporting secrets to .env.doppler ---"
doppler secrets download --no-file --format env > .env.doppler
echo "✓ Secrets exported"

# ── 4. Database ───────────────────────────────────────────────────────────────

echo "--- Starting database ---"
docker compose up wins-db -d

echo -n "    Waiting for DB to be healthy"
for i in $(seq 1 20); do
    if docker inspect --format='{{.State.Health.Status}}' wins-db 2>/dev/null | grep -q "healthy"; then
        echo " ✓"
        break
    fi
    echo -n "."
    sleep 2
    if [ "$i" -eq 20 ]; then
        echo ""
        echo "ERROR: DB did not become healthy in 40s. Check: docker logs wins-db"
        exit 1
    fi
done

# ── 5. Smoke test — one mock cycle ────────────────────────────────────────────

echo "--- Running mock cycle smoke test ---"

DB_URL=$(doppler secrets get DATABASE_URL --plain)
# Remap Docker-internal hostname to localhost for local execution
LOCAL_DB_URL="${DB_URL/wins-db:5432/localhost:5433}"

USE_MOCK_BRAIN=true \
TRADE_MODE=paper \
DATABASE_URL="$LOCAL_DB_URL" \
doppler run -- python -c "
import asyncio, os
os.environ['DATABASE_URL'] = '$LOCAL_DB_URL'
os.environ['USE_MOCK_BRAIN'] = 'true'
os.environ['TRADE_MODE'] = 'paper'
from wins.brain.cycle import run_cycle
asyncio.run(run_cycle())
print('Smoke test passed.')
"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Verify ANTHROPIC_API_KEY is set in Doppler (wins / dev config)"
echo "  2. Run one real cycle:  USE_MOCK_BRAIN=false doppler run -- python scripts/run_cycle.py --verbose"
echo "  3. Check decision_log:  docker exec wins-db psql -U wins -d wins -c 'SELECT token, action, model_used, cache_read_tokens FROM decision_log ORDER BY id DESC LIMIT 5;'"
echo "  4. When cache_read_tokens > 0 on call #2, expand TARGET_TOKENS in wins/shared/config.py"
echo ""
