"""
wins/execution/paper_portfolio.py
Tracks open paper positions between cycles.
On each cycle, checks current prices against stop-loss and target prices
and simulates fills — no exchange API needed.

Position state is persisted in trade_log (ts_close=NULL = open).
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone

import asyncpg

from wins.shared.logger import get_logger

log = get_logger("paper_portfolio")


@dataclass
class OpenPosition:
    trade_id:        int
    token:           str
    qty:             Decimal
    entry_price:     Decimal
    stop_loss_price: Decimal
    target_price:    Decimal
    cost_usd:        Decimal       # qty × entry_price


async def load_open_positions(pool: asyncpg.Pool) -> list[OpenPosition]:
    rows = await pool.fetch(
        """SELECT id, token, qty, entry_price, stop_loss_price, target_price
             FROM trade_log
            WHERE ts_close IS NULL AND side = 'buy'
         ORDER BY ts_open ASC"""
    )
    return [
        OpenPosition(
            trade_id        = r["id"],
            token           = r["token"],
            qty             = Decimal(str(r["qty"])),
            entry_price     = Decimal(str(r["entry_price"])),
            stop_loss_price = Decimal(str(r["stop_loss_price"])),
            target_price    = Decimal(str(r["target_price"])),
            cost_usd        = Decimal(str(r["qty"])) * Decimal(str(r["entry_price"])),
        )
        for r in rows
    ]


async def check_and_close_positions(
    pool:           asyncpg.Pool,
    current_prices: dict[str, Decimal],
) -> list[dict]:
    """
    Compare open positions against current prices.
    Closes any that hit stop-loss or target price.
    Returns list of closed position summaries (for alerting).
    """
    positions = await load_open_positions(pool)
    closed: list[dict] = []

    for pos in positions:
        price = current_prices.get(pos.token)
        if price is None:
            log.warning(f"No current price for open position {pos.token} — skipping SL/TP check.")
            continue

        exit_price: Decimal | None = None
        exit_reason: str | None = None

        if price <= pos.stop_loss_price:
            # Use actual current price (not SL price) — gap-downs fill at market, not limit
            exit_price  = price
            exit_reason = "stop_loss"
        elif price >= pos.target_price:
            exit_price  = pos.target_price
            exit_reason = "target"

        if exit_price and exit_reason:
            pnl_usd = (exit_price - pos.entry_price) * pos.qty
            pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price) * Decimal("100")

            await pool.execute(
                """UPDATE trade_log
                      SET ts_close    = $1,
                          exit_price  = $2,
                          pnl_usd     = $3,
                          pnl_pct     = $4,
                          exit_reason = $5
                    WHERE id = $6""",
                datetime.now(timezone.utc),
                float(exit_price),
                float(pnl_usd),
                float(pnl_pct),
                exit_reason,
                pos.trade_id,
            )

            log.info(
                f"[PAPER CLOSE] {pos.token} via {exit_reason}: "
                f"entry=${pos.entry_price} exit=${exit_price} "
                f"PnL=${pnl_usd:.2f} ({pnl_pct:.2f}%)"
            )

            closed.append({
                "token":       pos.token,
                "exit_reason": exit_reason,
                "exit_price":  float(exit_price),
                "pnl_usd":     float(pnl_usd),
                "pnl_pct":     float(pnl_pct),
                "qty":         float(pos.qty),
                "cost_usd":    float(pos.cost_usd),
            })

    return closed


def current_portfolio_value(
    positions:      list[OpenPosition],
    current_prices: dict[str, Decimal],
) -> Decimal:
    """Mark-to-market value of all open positions."""
    total = Decimal("0")
    for pos in positions:
        price = current_prices.get(pos.token, pos.entry_price)
        total += pos.qty * price
    return total
