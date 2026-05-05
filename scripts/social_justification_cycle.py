"""
scripts/social_justification_cycle.py

Daily cycle for the social-justification paper-trade strategy.
Isolated from the main bot — writes only to the `sj_trades` table.

Setup logic (long-only, derived from 730-day backtest 2024-05 → 2026-05):
    LONG when:
      - social_dominance z-score (vs trailing 30d) < -2
      - prior 7d price return is flat (|r7| <= 5%)
      - no existing open sj_trade for this token
    Hold 7 calendar days, exit at the price_close of the first available
    social_history row on or after planned_exit_date.

Backtested median 7d return on this setup: +2.7% (n=284 across 27 tokens / 2yr).
Live cohort tracking is what we are testing here.

Usage:
    python scripts/social_justification_cycle.py
    python scripts/social_justification_cycle.py --dry-run
"""
import argparse
import asyncio
import os
import sys
from datetime import timedelta

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

Z_THRESH   = -2.0
FLAT_BAND  = 0.05
HOLD_DAYS  = 7


SCAN_SQL = """
WITH base AS (
  SELECT
    token, date, social_dominance, price_close,
    AVG(social_dominance) OVER w_30 AS sd_ma30,
    STDDEV(social_dominance) OVER w_30 AS sd_sd30,
    LAG(price_close, 7) OVER w_seq AS price_7d_ago
  FROM social_history
  WHERE social_dominance IS NOT NULL
  WINDOW
    w_seq AS (PARTITION BY token ORDER BY date),
    w_30  AS (PARTITION BY token ORDER BY date ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING)
), latest AS (
  SELECT DISTINCT ON (token) *
  FROM base
  ORDER BY token, date DESC
)
SELECT token, date, price_close,
       (social_dominance - sd_ma30) / NULLIF(sd_sd30, 0) AS z,
       price_close / NULLIF(price_7d_ago, 0) - 1         AS prior_r7
FROM latest
WHERE sd_sd30 > 0 AND price_7d_ago IS NOT NULL AND price_close IS NOT NULL;
"""

OPEN_INSERT = """
INSERT INTO sj_trades
    (token, entry_date, entry_price, entry_z, entry_prior_r7, planned_exit_date, notes)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (token, entry_date) DO NOTHING
"""

OPEN_TOKEN_CHECK = "SELECT 1 FROM sj_trades WHERE token = $1 AND status = 'open' LIMIT 1"

CLOSE_DUE_QUERY = """
SELECT id, token, entry_date, entry_price, planned_exit_date
FROM sj_trades
WHERE status = 'open' AND planned_exit_date <= $1
"""

EXIT_PRICE_QUERY = """
SELECT date, price_close
FROM social_history
WHERE token = $1 AND date >= $2 AND price_close IS NOT NULL
ORDER BY date ASC
LIMIT 1
"""

CLOSE_UPDATE = """
UPDATE sj_trades
SET status     = 'closed',
    exit_date  = $2,
    exit_price = $3,
    pnl_pct    = ($3 - entry_price) / NULLIF(entry_price, 0),
    notes      = COALESCE(notes, '') || $4
WHERE id = $1
"""


async def close_due(conn, today, dry_run: bool) -> int:
    rows = await conn.fetch(CLOSE_DUE_QUERY, today)
    closed = 0
    for r in rows:
        price_row = await conn.fetchrow(EXIT_PRICE_QUERY, r["token"], r["planned_exit_date"])
        if price_row is None:
            print(f"  HOLD  {r['token']:<6} id={r['id']}: no price for {r['planned_exit_date']} or later yet")
            continue
        exit_date  = price_row["date"]
        exit_price = float(price_row["price_close"])
        entry      = float(r["entry_price"])
        pnl_pct    = (exit_price - entry) / entry
        held       = (exit_date - r["entry_date"]).days
        note       = f" | exit_close={exit_price:.6f} pnl={pnl_pct:+.2%}"
        print(f"  CLOSE {r['token']:<6} id={r['id']}  held={held}d  pnl={pnl_pct:+.2%}")
        if not dry_run:
            await conn.execute(CLOSE_UPDATE, r["id"], exit_date, exit_price, note)
        closed += 1
    return closed


async def open_setups(conn, dry_run: bool) -> int:
    rows = await conn.fetch(SCAN_SQL)
    opened = 0
    for r in rows:
        z, prior_r7 = r["z"], r["prior_r7"]
        if z is None or prior_r7 is None:
            continue
        if z >= Z_THRESH or abs(prior_r7) > FLAT_BAND:
            continue
        if await conn.fetchval(OPEN_TOKEN_CHECK, r["token"]):
            print(f"  skip  {r['token']:<6} (existing open position)  z={z:.2f}")
            continue
        planned_exit = r["date"] + timedelta(days=HOLD_DAYS)
        note = f"z={z:.2f}, prior_r7={prior_r7:+.2%}, entry_close={r['price_close']:.6f}"
        print(f"  OPEN  {r['token']:<6} {r['date']}  {note}  exit_target={planned_exit}")
        if not dry_run:
            await conn.execute(
                OPEN_INSERT,
                r["token"], r["date"], float(r["price_close"]),
                float(z), float(prior_r7), planned_exit, note,
            )
        opened += 1
    return opened


async def main(dry_run: bool) -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set"); sys.exit(1)
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        today = await conn.fetchval("SELECT MAX(date) FROM social_history")
        if today is None:
            print("ERROR: social_history is empty — run the ingest first"); sys.exit(1)
        print(f"social-justification cycle  asof={today}  dry_run={dry_run}")
        print(f"--- closing due positions (planned_exit <= {today}) ---")
        n_closed = await close_due(conn, today, dry_run)
        print(f"closed: {n_closed}")
        print(f"--- scanning for new long-fade entries (z<{Z_THRESH}, |r7|<={FLAT_BAND:.0%}) ---")
        n_opened = await open_setups(conn, dry_run)
        print(f"opened: {n_opened}")
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="log decisions without writing to DB")
    args = p.parse_args()
    asyncio.run(main(args.dry_run))
