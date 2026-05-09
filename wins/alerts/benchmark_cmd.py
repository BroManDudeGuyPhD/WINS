"""
wins/alerts/benchmark_cmd.py
CLI: python -m wins.alerts.benchmark_cmd

Fetches open positions, prices them via CoinGecko, computes returns vs BTC
since each trade opened, then sends a Discord DM with the alpha report.
"""
import asyncio
from decimal import Decimal

import httpx

from wins.shared.db import get_pool, close_pool
from wins.ingestion.collector import fetch_prices
from wins.alerts.discord_bot import alert_benchmark_report


async def run() -> None:
    pool = await get_pool()

    rows = await pool.fetch(
        """SELECT token, qty, entry_price, btc_price_at_entry, ts_open
             FROM trade_log
            WHERE ts_close IS NULL AND side = 'buy'
         ORDER BY ts_open"""
    )

    if not rows:
        from wins.alerts.discord_bot import send_message
        await send_message("No open positions to benchmark.")
        await close_pool()
        return

    tokens = list({r["token"] for r in rows})
    fetch_symbols = tokens if "BTC" in tokens else tokens + ["BTC"]

    async with httpx.AsyncClient() as client:
        snapshots = await fetch_prices(client, fetch_symbols)

    btc_snap = snapshots.get("BTC")
    btc_now  = float(btc_snap.price_usd) if btc_snap else None

    positions = []
    for r in rows:
        token  = r["token"]
        snap   = snapshots.get(token)
        if not snap:
            continue

        entry   = float(r["entry_price"])
        current = float(snap.price_usd)
        token_pct = (current - entry) / entry * 100 if entry > 0 else None

        btc_entry_raw = r["btc_price_at_entry"]
        btc_entry = float(btc_entry_raw) if btc_entry_raw else None
        btc_pct   = (btc_now - btc_entry) / btc_entry * 100 if (btc_now and btc_entry and btc_entry > 0) else None
        alpha_pct = token_pct - btc_pct if (token_pct is not None and btc_pct is not None) else None

        positions.append({
            "token":     token,
            "entry":     entry,
            "current":   current,
            "token_pct": token_pct,
            "btc_pct":   btc_pct,
            "alpha_pct": alpha_pct,
            "ts_open":   r["ts_open"],
        })

    await alert_benchmark_report(positions, btc_now)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(run())
