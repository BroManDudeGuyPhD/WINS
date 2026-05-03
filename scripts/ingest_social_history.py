"""
scripts/ingest_social_history.py
Bulk-ingest 730 days of LunarCrush daily social + price data into social_history table.

Fetches time-series data for all TARGET_TOKENS + MACRO_TOKENS, aggregates hourly
records to daily, and upserts into the DB. Safe to re-run — ON CONFLICT DO UPDATE
overwrites existing rows with fresher data.

Run time: ~10-15 min (rate-limit sleep between tokens). Leave running.

Usage:
    python scripts/ingest_social_history.py
    python scripts/ingest_social_history.py --days 365    # shorter window
    python scripts/ingest_social_history.py SOL SUI       # specific tokens only
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import httpx
import asyncpg

LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"
LUNARCRUSH_KEY  = os.environ.get("LUNARCRUSH_API_KEY", "")
DATABASE_URL    = os.environ.get("DATABASE_URL", "")

TARGET_TOKENS = ["SOL", "SUI", "JUP", "ARB", "LINK"]
MACRO_TOKENS  = ["BTC", "ETH"]
ALL_TOKENS    = TARGET_TOKENS + MACRO_TOKENS

RATE_LIMIT_SLEEP = 6.5   # LunarCrush allows ~10 req/min
DEFAULT_DAYS     = 730


# ─── LunarCrush fetch ─────────────────────────────────────────────────────────

def _fetch_timeseries(symbol: str, days: int) -> list[dict]:
    """Fetch hourly time-series for `symbol` going back `days` days. Returns raw rows."""
    if not LUNARCRUSH_KEY:
        print("  ERROR: LUNARCRUSH_API_KEY not set")
        return []

    end_ts   = int(datetime.now(timezone.utc).timestamp())
    start_ts = end_ts - days * 86400

    for attempt in range(4):
        try:
            resp = httpx.get(
                f"{LUNARCRUSH_BASE}/coins/{symbol.upper()}/time-series/v2",
                headers={"Authorization": f"Bearer {LUNARCRUSH_KEY}"},
                params={"start": start_ts, "end": end_ts},
                timeout=60,
            )
            break
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            wait = (2 ** attempt) * 5
            print(f"  [LC] {symbol} connection error (attempt {attempt+1}/4): {exc} — retrying in {wait}s")
            time.sleep(wait)
    else:
        print(f"  [LC] {symbol}: all retries exhausted")
        return []

    if resp.status_code == 429:
        print(f"  [LC] {symbol} rate limited — waiting 60s")
        time.sleep(60)
        return _fetch_timeseries(symbol, days)

    if resp.status_code != 200:
        print(f"  [LC] {symbol} HTTP {resp.status_code}: {resp.text[:200]}")
        return []

    data = resp.json().get("data", [])
    if not data:
        print(f"  [LC] {symbol}: empty data response")
    return data


def _aggregate_to_daily(hourly_rows: list[dict]) -> list[dict]:
    """
    Aggregate hourly LunarCrush rows to daily OHLCV + social metrics.
    Returns list of dicts keyed by date string (YYYY-MM-DD).
    """
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in hourly_rows:
        ts = row.get("time") or row.get("ts")
        if ts is None:
            continue
        date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        by_date[date_str].append(row)

    daily = []
    for date_str, rows in sorted(by_date.items()):
        opens  = [r["open"]  for r in rows if r.get("open")  is not None]
        closes = [r["close"] for r in rows if r.get("close") is not None]
        highs  = [r["high"]  for r in rows if r.get("high")  is not None]
        lows   = [r["low"]   for r in rows if r.get("low")   is not None]
        vols   = [r.get("volume_24h") or r.get("volume", 0) for r in rows]

        # Social metrics: take last non-null value of the day (most recent snapshot)
        def last_val(key: str):
            vals = [r[key] for r in rows if r.get(key) is not None]
            return vals[-1] if vals else None

        def avg_val(key: str):
            vals = [r[key] for r in rows if r.get(key) is not None]
            return sum(vals) / len(vals) if vals else None

        daily.append({
            "date":             datetime.strptime(date_str, "%Y-%m-%d").date(),
            "price_open":       opens[0]  if opens  else None,
            "price_close":      closes[-1] if closes else None,
            "price_high":       max(highs) if highs  else None,
            "price_low":        min(lows)  if lows   else None,
            "volume_24h":       max(vols)  if vols   else None,
            "social_dominance": avg_val("social_dominance"),
            "interactions_24h": last_val("interactions_24h"),
            "sentiment":        avg_val("sentiment"),
            "galaxy_score":     avg_val("galaxy_score"),
            "alt_rank":         last_val("alt_rank"),
        })
    return daily


# ─── DB upsert ────────────────────────────────────────────────────────────────

async def upsert_daily_rows(pool: asyncpg.Pool, symbol: str, rows: list[dict]) -> int:
    """Upsert daily rows into social_history. Returns number of rows written."""
    if not rows:
        return 0

    records = [
        (
            symbol.upper(),
            row["date"],
            row.get("social_dominance"),
            row.get("interactions_24h"),
            row.get("sentiment"),
            row.get("galaxy_score"),
            row.get("alt_rank"),
            row.get("price_open"),
            row.get("price_close"),
            row.get("price_high"),
            row.get("price_low"),
            row.get("volume_24h"),
        )
        for row in rows
    ]

    await pool.executemany(
        """
        INSERT INTO social_history
            (token, date, social_dominance, interactions_24h, sentiment,
             galaxy_score, alt_rank, price_open, price_close, price_high, price_low, volume_24h)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (token, date) DO UPDATE SET
            social_dominance = EXCLUDED.social_dominance,
            interactions_24h = EXCLUDED.interactions_24h,
            sentiment        = EXCLUDED.sentiment,
            galaxy_score     = EXCLUDED.galaxy_score,
            alt_rank         = EXCLUDED.alt_rank,
            price_open       = EXCLUDED.price_open,
            price_close      = EXCLUDED.price_close,
            price_high       = EXCLUDED.price_high,
            price_low        = EXCLUDED.price_low,
            volume_24h       = EXCLUDED.volume_24h,
            ts               = NOW()
        """,
        records,
    )
    return len(records)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(tokens: list[str], days: int) -> None:
    if not LUNARCRUSH_KEY:
        print("ERROR: LUNARCRUSH_API_KEY not set in environment.")
        sys.exit(1)
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set in environment.")
        sys.exit(1)

    print(f"Connecting to database...")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)

    print(f"Ingesting {days} days of social history for {len(tokens)} tokens: {tokens}")
    print(f"Rate limit: {RATE_LIMIT_SLEEP}s between requests\n")

    total_rows = 0
    for i, symbol in enumerate(tokens):
        print(f"[{i+1}/{len(tokens)}] {symbol} — fetching {days} days...")
        hourly = _fetch_timeseries(symbol, days)
        if not hourly:
            print(f"  Skipping {symbol}: no data returned")
        else:
            daily = _aggregate_to_daily(hourly)
            n = await upsert_daily_rows(pool, symbol, daily)
            total_rows += n
            # Quick coverage summary
            dom_count = sum(1 for r in daily if r.get("social_dominance") is not None)
            print(f"  Stored {n} daily rows, {dom_count}/{n} have social_dominance")

        if i < len(tokens) - 1:
            print(f"  Sleeping {RATE_LIMIT_SLEEP}s (rate limit)...")
            time.sleep(RATE_LIMIT_SLEEP)

    await pool.close()
    print(f"\nDone. Total rows upserted: {total_rows}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens", nargs="*", help="Token symbols to ingest (default: all)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Days of history to fetch")
    args = parser.parse_args()

    tokens = [t.upper() for t in args.tokens] if args.tokens else ALL_TOKENS
    asyncio.run(main(tokens, args.days))
