#!/usr/bin/env bash
# scripts/social_justification_daily.sh
#
# Daily wrapper for the social-justification paper-trade strategy.
#   1. Refresh the last 3 days of social_history from LunarCrush
#   2. Run the social-justification cycle (open new entries, close due exits)
#   3. Print a short report for the log
#
# Suggested cron (host crontab on the box running wins-execution):
#   30 1 * * * /home/concord/wins-runner/_work/WINS/WINS/scripts/social_justification_daily.sh \
#               >> /var/log/wins-sj.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER=wins-execution
TOKENS="AAVE ALGO APT ARB ATOM AVAX BONK BTC CRV DOT DYDX ETH FTM GMX INJ JUP LDO LINK NEAR OP PENDLE PYTH SNX SOL SUI UNI WIF"

echo "=== sj-daily $(date -u +%FT%TZ) ==="

# Sync the latest scripts into the container (idempotent; cheap)
docker cp scripts/ingest_social_history.py        "$CONTAINER":/app/ingest_social_history.py
docker cp scripts/social_justification_cycle.py   "$CONTAINER":/app/social_justification_cycle.py
docker cp scripts/social_justification_report.py  "$CONTAINER":/app/social_justification_report.py

echo "--- ingest (3-day refresh window) ---"
docker exec "$CONTAINER" python /app/ingest_social_history.py --days 3 $TOKENS

echo "--- cycle ---"
docker exec "$CONTAINER" python /app/social_justification_cycle.py

echo "--- report ---"
docker exec "$CONTAINER" python /app/social_justification_report.py

echo "=== sj-daily done ==="
