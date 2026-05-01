"""
scripts/run_test_trade.py
Fires a complete paper trade cycle through Discord:
  open → (wait) → target hit → close → health check
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TRADE_MODE",     "paper")
os.environ.setdefault("USE_MOCK_BRAIN", "true")
os.environ.setdefault("LOG_LEVEL",      "WARNING")

from dotenv import load_dotenv
load_dotenv()

from wins.alerts.discord_bot import (
    alert_trade_opened,
    alert_trade_closed,
    alert_system_health,
    send_message,
)

TOKEN     = "SOL"
ENTRY     = 83.00
SL        = 73.04     # -12%
TP        = 103.75    # +25%
SIZE_USD  = 50.00
QTY       = SIZE_USD / ENTRY


async def run() -> None:
    print("Sending: trade open alert...")
    await send_message("**[PAPER] Test trade cycle starting...**")
    await alert_trade_opened(
        TOKEN, "buy", ENTRY, SL, TP, SIZE_USD, 0.72,
        (
            "SOL breaking above 4h resistance on strong volume. "
            "BTC macro neutral. LunarCrush sentiment elevated. "
            "Entering long with 12% SL and 25% target."
        ),
        "paper",
    )
    print("Sending: target hit / close alert...")
    await asyncio.sleep(1)

    exit_price = TP
    pnl_usd    = (exit_price - ENTRY) * QTY
    pnl_pct    = (exit_price - ENTRY) / ENTRY * 100
    await alert_trade_closed(TOKEN, pnl_usd, pnl_pct, "target_hit", "paper")

    print("Sending: post-trade health check...")
    await asyncio.sleep(1)

    capital_after = 100.00 + pnl_usd
    await alert_system_health(capital_after, 0, "paper", "paper", cycle_count=1)

    print(f"Done. Net P&L: +{pnl_usd:.2f} USD ({pnl_pct:+.1f}%) — capital now ${capital_after:.2f}")


if __name__ == "__main__":
    asyncio.run(run())
