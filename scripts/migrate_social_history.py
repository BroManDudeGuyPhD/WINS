"""
scripts/migrate_social_history.py
One-time migration: creates social_history table on an existing WINS database.
Safe to re-run — uses IF NOT EXISTS.

Requires only stdlib + psql (no asyncpg) so it runs on bare CI runners.

Usage:
    python scripts/migrate_social_history.py
"""
import os
import subprocess
import sys
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "") or (sys.argv[1] if len(sys.argv) > 1 else "")

DDL = """\
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
"""


def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set in environment.")
        sys.exit(1)

    u = urlparse(DATABASE_URL)
    env = {**os.environ, "PGPASSWORD": u.password or ""}
    cmd = [
        "psql",
        "--host", u.hostname,
        "--port", str(u.port or 5432),
        "--username", u.username,
        "--dbname", u.path.lstrip("/"),
        "--command", DDL,
    ]

    print(f"Connecting to {u.hostname}:{u.port or 5432}/{u.path.lstrip('/')} as {u.username}...")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)

    print("Migration complete.")


if __name__ == "__main__":
    main()
