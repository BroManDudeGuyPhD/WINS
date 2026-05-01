"""
scripts/test_binance_testnet.py
Validates the full Binance execution path against the testnet:
  - Account balance check
  - Live price fetch
  - Market BUY (small USDT amount)
  - Stop-loss order placement
  - Cancel stop-loss order
  - Market SELL to close position

Requires testnet API keys from https://testnet.binance.vision
(Sign in with GitHub — free, instant, ~10,000 USDT test balance)

Add to .env:
    BINANCE_TESTNET_API_KEY=your_testnet_key
    BINANCE_TESTNET_API_SECRET=your_testnet_secret

Usage:
    python scripts/test_binance_testnet.py
    python scripts/test_binance_testnet.py --token BTC   # default: SOL
    python scripts/test_binance_testnet.py --size 20     # USDT to spend (default: 15)
"""
from __future__ import annotations
import asyncio
import argparse
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("LOG_LEVEL", "INFO")

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich import box

from wins.shared.config import BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET

console = Console()


def _check_step(label: str, ok: bool, detail: str = "") -> None:
    icon  = "[bold green]✓[/bold green]" if ok else "[bold red]✗[/bold red]"
    extra = f"  [dim]{detail}[/dim]" if detail else ""
    console.print(f"  {icon}  {label}{extra}")


async def run(token: str, size_usd: Decimal) -> None:
    console.print()
    console.print(Panel(
        f"  Token: [bold white]{token}[/bold white]  ·  "
        f"Size: [yellow]${size_usd}[/yellow]  ·  "
        f"[dim]Binance Testnet — no real funds[/dim]",
        title="[bold cyan]WINS Binance Testnet Validation[/bold cyan]",
        border_style="cyan",
        width=65,
    ))

    if not BINANCE_TESTNET_API_KEY or not BINANCE_TESTNET_API_SECRET:
        console.print(Panel(
            "  [red]BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET not set.[/red]\n\n"
            "  1. Go to [link=https://testnet.binance.vision]https://testnet.binance.vision[/link]\n"
            "  2. Log in with GitHub\n"
            "  3. Generate an API key\n"
            "  4. Add to .env:\n"
            "     [bold]BINANCE_TESTNET_API_KEY=...[/bold]\n"
            "     [bold]BINANCE_TESTNET_API_SECRET=...[/bold]",
            title="[red]Missing credentials[/red]",
            border_style="red",
            width=65,
        ))
        return

    from wins.execution.exchange.binance_api import BinanceClient
    client = BinanceClient(testnet=True)

    results: list[tuple[str, bool, str]] = []

    # ── Step 1: Account balance ────────────────────────────────────────────────
    console.print()
    console.print(Rule("[dim]Step 1 — Account balance[/dim]", style="dim"))
    try:
        balance = await client.get_balance()
        ok = balance.available_usd >= size_usd
        results.append(("Balance check", ok, f"${balance.available_usd:.2f} USDT available"))
        _check_step("Account balance", ok, f"${balance.available_usd:.2f} USDT available")
        if not ok:
            console.print(f"  [red]Need at least ${size_usd} USDT — testnet balance too low.[/red]")
            return
    except Exception as exc:
        results.append(("Balance check", False, str(exc)))
        _check_step("Account balance", False, str(exc))
        console.print("  [red]Cannot proceed — check your testnet API credentials.[/red]")
        return

    # ── Step 2: Live price ─────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[dim]Step 2 — Price fetch[/dim]", style="dim"))
    try:
        price = await client.get_ticker_price(token)
        results.append(("Price fetch", True, f"${price}"))
        _check_step(f"{token} price", True, f"${price}")
    except Exception as exc:
        results.append(("Price fetch", False, str(exc)))
        _check_step(f"{token} price", False, str(exc))
        return

    sl_price = price * Decimal("0.88")   # 12% below
    tp_price = price * Decimal("1.25")   # 25% above

    # ── Step 3: Market BUY ─────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[dim]Step 3 — Market BUY[/dim]", style="dim"))
    try:
        buy = await client.place_market_buy(token, size_usd)
        ok  = buy.qty > 0
        results.append(("Market BUY", ok, f"order_id={buy.order_id}  qty={buy.qty}  fill=${buy.fill_price}"))
        _check_step("Market BUY", ok, f"order_id={buy.order_id}  qty={buy.qty}  fill=${buy.fill_price}")
    except Exception as exc:
        results.append(("Market BUY", False, str(exc)))
        _check_step("Market BUY", False, str(exc))
        return

    # ── Step 4: Place stop-loss ────────────────────────────────────────────────
    console.print()
    console.print(Rule("[dim]Step 4 — Place stop-loss order[/dim]", style="dim"))
    sl_order_id: str | None = None
    try:
        sl = await client.place_stop_loss(token, buy.qty, sl_price)
        sl_order_id = sl.order_id
        results.append(("Stop-loss order", True, f"order_id={sl.order_id}  stop=${sl_price:.4f}"))
        _check_step("Stop-loss placed", True, f"order_id={sl.order_id}  stop=${sl_price:.4f}")
    except Exception as exc:
        results.append(("Stop-loss order", False, str(exc)))
        _check_step("Stop-loss placed", False, str(exc))

    # ── Step 5: Cancel stop-loss ───────────────────────────────────────────────
    console.print()
    console.print(Rule("[dim]Step 5 — Cancel stop-loss (before market sell)[/dim]", style="dim"))
    if sl_order_id:
        try:
            cancelled = await client.cancel_order(sl_order_id, token)
            results.append(("Cancel stop-loss", cancelled, f"order_id={sl_order_id}"))
            _check_step("Stop-loss cancelled", cancelled, f"order_id={sl_order_id}")
        except Exception as exc:
            results.append(("Cancel stop-loss", False, str(exc)))
            _check_step("Cancel stop-loss", False, str(exc))
    else:
        _check_step("Cancel stop-loss", False, "skipped — no SL order placed")

    # ── Step 6: Market SELL ────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[dim]Step 6 — Market SELL (close position)[/dim]", style="dim"))
    try:
        sell = await client.place_market_sell(token, buy.qty)
        ok   = sell.qty > 0
        pnl  = (sell.fill_price - buy.fill_price) * sell.qty
        results.append(("Market SELL", ok, f"order_id={sell.order_id}  fill=${sell.fill_price}  slippage_pnl={pnl:+.4f}"))
        _check_step("Market SELL", ok, f"fill=${sell.fill_price}  slippage/pnl={pnl:+.4f} USD")
    except Exception as exc:
        results.append(("Market SELL", False, str(exc)))
        _check_step("Market SELL", False, str(exc))

    # ── Final summary ──────────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold white]Results[/bold white]", style="cyan"))
    console.print()

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("Step",   style="white")
    table.add_column("Status", justify="center")
    table.add_column("Detail", style="dim")

    all_ok = True
    for label, ok, detail in results:
        all_ok = all_ok and ok
        table.add_row(
            label,
            Text("PASS", style="bold green") if ok else Text("FAIL", style="bold red"),
            detail,
        )
    console.print(table)

    summary_style = "green" if all_ok else "red"
    verdict = "All steps passed — Binance execution layer is ready." if all_ok \
              else "Some steps failed — review errors above before going live."
    console.print(Panel(
        f"  [{summary_style}][bold]{verdict}[/bold][/{summary_style}]",
        border_style=summary_style,
        width=65,
    ))
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="WINS Binance testnet validator")
    parser.add_argument("--token", default="SOL",  help="Token to test with (default: SOL)")
    parser.add_argument("--size",  type=float, default=15.0, help="USDT trade size (default: 15)")
    args = parser.parse_args()
    asyncio.run(run(args.token, Decimal(str(args.size))))


if __name__ == "__main__":
    main()
