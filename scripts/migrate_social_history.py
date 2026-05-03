"""
scripts/migrate_social_history.py
One-time migration: creates social_history table on an existing WINS database.
Safe to re-run — uses IF NOT EXISTS.

Usage:
    python scripts/migrate_social_history.py
"""
import asyncio
import os
import sys

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

DDL = """
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
"""


async def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set in environment.")
        sys.exit(1)

    print("Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(DDL)
        print("social_history table created (or already exists).")
        count = await conn.fetchval("SELECT COUNT(*) FROM social_history")
        print(f"Current row count: {count}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
