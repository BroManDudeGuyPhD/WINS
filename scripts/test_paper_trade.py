#!/usr/bin/env python
"""
scripts/test_paper_trade.py
In-memory paper trade simulator.

Runs a complete buy → hold → close cycle using real entry prices
from CoinGecko (or synthetic prices with --synthetic) and simulated
future price movement. No database or API keys required.

Usage:
    # Fully synthetic scenarios (fastest, no network):
    python scripts/test_paper_trade.py --synthetic

    # Real entry prices + simulated future (needs CoinGecko, no key required):
    python scripts/test_paper_trade.py

    # Custom scenarios:
    python scripts/test_paper_trade.py --synthetic --ticks 20

    # Slow mode — pause between ticks to watch live:
    python scripts/test_paper_trade.py --synthetic --speed slow
"""
import asyncio
import argparse
import os
import sys
import time
import random
from dataclasses import dataclass, field
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TRADE_MODE",      "paper")
os.environ.setdefault("USE_MOCK_BRAIN",  "true")
os.environ.setdefault("LOG_LEVEL",       "WARNING")

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.panel import Panel
from rich.rule import Rule
from rich.live import Live
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich import box

console = Console()

# ─── Scenario definitions ─────────────────────────────────────────────────────

@dataclass
class Scenario:
    token:          str
    entry_price:    float
    sl_pct:         float        # stop loss as fraction below entry (e.g. 0.12 = 12%)
    tp_pct:         float        # target as fraction above entry  (e.g. 0.25 = 25%)
    price_path:     list[float]  # list of price % changes each tick (positive = up)
    label:          str          # human description of the scenario

    @property
    def sl_price(self)  -> float: return self.entry_price * (1 - self.sl_pct)
    @property
    def tp_price(self)  -> float: return self.entry_price * (1 + self.tp_pct)
    @property
    def size_usd(self)  -> float: return 50.0   # half of $100 capital


SYNTHETIC_SCENARIOS: list[Scenario] = [
    Scenario(
        token       = "SOL",
        entry_price = 83.00,
        sl_pct      = 0.12,
        tp_pct      = 0.25,
        price_path  = [0.5, 1.2, 2.1, 3.4, 5.0, 7.8, 10.2, 14.0, 18.5, 25.1],
        label       = "Momentum breakout — hits target",
    ),
    Scenario(
        token       = "ARB",
        entry_price = 0.125,
        sl_pct      = 0.12,
        tp_pct      = 0.25,
        price_path  = [0.3, -1.5, -3.2, -6.0, -8.8, -12.1],
        label       = "False breakout — hits stop loss",
    ),
    Scenario(
        token       = "LINK",
        entry_price = 9.14,
        sl_pct      = 0.12,
        tp_pct      = 0.25,
        price_path  = [1.0, 2.5, 4.0, 3.2, 1.8, 4.5, 8.0, 12.0, 18.0, 25.5],
        label       = "Volatile climb — hits target after pullback",
    ),
    Scenario(
        token       = "INJ",
        entry_price = 12.50,
        sl_pct      = 0.12,
        tp_pct      = 0.25,
        price_path  = [2.0, 3.5, 2.1, 1.0, 0.5, -0.5, -2.0, -3.5, -5.0, -7.0, -10.0, -12.5],
        label       = "Rally fades — hits stop loss after slow bleed",
    ),
]


# ─── In-memory trade state ─────────────────────────────────────────────────────

@dataclass
class Trade:
    token:          str
    entry_price:    float
    sl_price:       float
    tp_price:       float
    qty:            float            # tokens held
    cost_usd:       float            # USD spent
    label:          str
    ticks:          list[float] = field(default_factory=list)   # price at each tick
    exit_price:     float = 0.0
    exit_reason:    str = ""         # "target" | "stop_loss" | "open"
    closed:         bool = False

    @property
    def current_price(self) -> float:
        return self.ticks[-1] if self.ticks else self.entry_price

    @property
    def unrealised_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.qty

    @property
    def unrealised_pct(self) -> float:
        return (self.current_price - self.entry_price) / self.entry_price * 100

    @property
    def realised_pnl(self) -> float:
        if not self.closed: return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def realised_pct(self) -> float:
        if not self.closed: return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100


# ─── Tick-by-tick display ─────────────────────────────────────────────────────

def _price_bar(pct: float, width: int = 20) -> Text:
    """Render a small horizontal bar showing price % from entry."""
    clamped = max(-25.0, min(25.0, pct))
    filled  = int(abs(clamped) / 25.0 * width)
    bar     = ("█" * filled).ljust(width) if clamped >= 0 else ("█" * filled).rjust(width)
    style   = "green" if clamped >= 0 else "red"
    return Text(bar, style=style)


def _pnl_color(val: float) -> str:
    if val >  1.0: return "bold green"
    if val >  0:   return "green"
    if val < -1.0: return "bold red"
    if val <  0:   return "red"
    return "dim"


def _build_live_table(trades: list[Trade], tick: int, total_ticks: int) -> Table:
    table = Table(
        title=f"[bold cyan]WINS Paper Trading — Tick {tick}/{total_ticks}[/bold cyan]",
        box=box.ROUNDED,
        show_footer=True,
        header_style="bold white",
    )
    table.add_column("Token",     style="bold white",   footer="")
    table.add_column("Entry",     justify="right",       footer="")
    table.add_column("Current",   justify="right",       footer="")
    table.add_column("SL",        justify="right", style="red",   footer="")
    table.add_column("Target",    justify="right", style="green", footer="")
    table.add_column("Move",      justify="center",      footer="")
    table.add_column("Unreal P&L",justify="right",       footer="Total")
    table.add_column("Status",    footer="")

    total_pnl = sum(t.unrealised_pnl if not t.closed else t.realised_pnl for t in trades)

    for t in trades:
        if t.closed:
            status = Text("✓ TARGET", style="bold green") if t.exit_reason == "target" \
                else Text("✗ STOPPED", style="bold red")
            pnl_val  = t.realised_pnl
            pnl_pct  = t.realised_pct
            cur_disp = Text(f"${t.exit_price:.4f}", style="dim")
        else:
            status   = Text("● OPEN", style="bold cyan")
            pnl_val  = t.unrealised_pnl
            pnl_pct  = t.unrealised_pct
            cur_disp = Text(f"${t.current_price:.4f}",
                            style="green" if t.current_price >= t.entry_price else "red")

        pnl_text = Text(
            f"{'+'if pnl_val>=0 else ''}{pnl_val:.2f} ({pnl_pct:+.1f}%)",
            style=_pnl_color(pnl_pct),
        )

        table.add_row(
            t.token,
            f"${t.entry_price:.4f}",
            cur_disp,
            f"${t.sl_price:.4f}",
            f"${t.tp_price:.4f}",
            _price_bar(pnl_pct),
            pnl_text,
            status,
        )

    total_style = _pnl_color(total_pnl)
    table.columns[6].footer = Text(f"{'+'if total_pnl>=0 else ''}{total_pnl:.2f}", style=total_style)
    return table


# ─── Simulation engine ────────────────────────────────────────────────────────

def _simulate(scenario: Scenario) -> Trade:
    trade = Trade(
        token       = scenario.token,
        entry_price = scenario.entry_price,
        sl_price    = scenario.sl_price,
        tp_price    = scenario.tp_price,
        qty         = scenario.size_usd / scenario.entry_price,
        cost_usd    = scenario.size_usd,
        label       = scenario.label,
    )
    return trade


def _tick_price(trade: Trade, pct_change: float) -> bool:
    """Apply one price tick. Returns True if position was closed."""
    if trade.closed:
        return False
    price = trade.entry_price * (1 + pct_change / 100)
    trade.ticks.append(price)

    if price <= trade.sl_price:
        trade.exit_price  = trade.sl_price
        trade.exit_reason = "stop_loss"
        trade.closed      = True
        return True
    if price >= trade.tp_price:
        trade.exit_price  = trade.tp_price
        trade.exit_reason = "target"
        trade.closed      = True
        return True
    return False


async def _run_live_prices_scenarios(ticks: int) -> list[Scenario]:
    """Fetch real entry prices and build scenarios around them."""
    from wins.ingestion.collector import collect_signal_bundles
    from wins.brain.decision import make_decision
    from wins.execution.risk import validate_decision, calculate_position_size
    from decimal import Decimal

    with console.status("[cyan]Fetching live prices...[/cyan]", spinner="dots"):
        bundles = await collect_signal_bundles()

    capital        = Decimal("100.00")
    open_positions = 0
    scenarios: list[Scenario] = []

    for b in bundles:
        decision = make_decision(b)
        if decision is None or decision.action.value != "buy":
            continue
        approved, _ = validate_decision(decision, capital, open_positions, capital)
        if not approved:
            continue

        pos_usd = float(calculate_position_size(capital, decision.entry_price))

        # Build a random price path constrained by SL/TP
        entry = float(decision.entry_price)
        sl_pct = (entry - float(decision.stop_loss_price)) / entry
        tp_pct = (float(decision.target_price) - entry) / entry

        # 50/50 chance the trade wins or loses — random walk toward one extreme
        wins = random.random() > 0.5
        path = _random_walk(ticks, tp_pct * 100 if wins else -sl_pct * 100)

        scenarios.append(Scenario(
            token       = b.token,
            entry_price = entry,
            sl_pct      = sl_pct,
            tp_pct      = tp_pct,
            price_path  = path,
            label       = f"Live price — {'win path' if wins else 'loss path'}",
        ))
        capital        -= Decimal(str(pos_usd))
        open_positions += 1
        if open_positions >= 2:
            break

    if not scenarios:
        console.print("[yellow]No live buy signals found — using synthetic scenarios.[/yellow]")
        return SYNTHETIC_SCENARIOS
    return scenarios


def _random_walk(n_ticks: int, target_pct: float) -> list[float]:
    """Generate a noisy path that trends toward target_pct."""
    path: list[float] = []
    current = 0.0
    for i in range(n_ticks):
        drift = target_pct / n_ticks
        noise = random.gauss(0, abs(target_pct) * 0.15)
        current += drift + noise
        path.append(round(current, 3))
    return path


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run(synthetic: bool, ticks: int, speed: str) -> None:
    delay = {"fast": 0.05, "normal": 0.25, "slow": 0.8}.get(speed, 0.25)

    if synthetic:
        scenarios = SYNTHETIC_SCENARIOS
    else:
        scenarios = await _run_live_prices_scenarios(ticks)

    console.print()
    console.print(Panel(
        f"  [bold white]{len(scenarios)} position(s)[/bold white]  ·  "
        f"[cyan]{ticks} ticks[/cyan]  ·  "
        f"[dim]speed={speed}  source={'synthetic' if synthetic else 'live prices'}[/dim]",
        title="[bold white]WINS Paper Trade Simulation[/bold white]",
        border_style="cyan",
        width=70,
    ))

    trades: list[Trade] = [_simulate(s) for s in scenarios]

    # Show entry summary
    entry_table = Table(box=box.SIMPLE, header_style="bold cyan", show_header=True)
    entry_table.add_column("Token",   style="bold white")
    entry_table.add_column("Entry",   justify="right")
    entry_table.add_column("SL",      justify="right", style="red")
    entry_table.add_column("Target",  justify="right", style="green")
    entry_table.add_column("R:R",     justify="right")
    entry_table.add_column("Size",    justify="right", style="yellow")
    entry_table.add_column("Scenario")

    for t, s in zip(trades, scenarios):
        rr = s.tp_pct / s.sl_pct
        entry_table.add_row(
            t.token,
            f"${t.entry_price:.4f}",
            f"${t.sl_price:.4f}",
            f"${t.tp_price:.4f}",
            Text(f"{rr:.1f}x", style="green" if rr >= 2 else "yellow"),
            f"${t.cost_usd:.2f}",
            f"[dim]{s.label}[/dim]",
        )
    console.print(entry_table)
    console.print(Rule("[dim]Simulation running[/dim]", style="dim"))

    # ── Run ticks ──────────────────────────────────────────────────────────────
    max_ticks = max(len(s.price_path) for s in scenarios)
    effective_ticks = min(ticks, max_ticks)

    with Live(console=console, refresh_per_second=20) as live:
        for tick_i in range(effective_ticks):
            for t, s in zip(trades, scenarios):
                if tick_i < len(s.price_path):
                    closed = _tick_price(t, s.price_path[tick_i])
                    if closed:
                        # freeze remaining ticks at exit price
                        pct_at_exit = (t.exit_price - t.entry_price) / t.entry_price * 100
                        while tick_i < effective_ticks - 1:
                            t.ticks.append(t.exit_price)
                            tick_i += 1

            live.update(_build_live_table(trades, tick_i + 1, effective_ticks))
            time.sleep(delay)

        # Final render
        live.update(_build_live_table(trades, effective_ticks, effective_ticks))

    # ── Results table ──────────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold white]Trade Results[/bold white]", style="cyan"))
    console.print()

    results = Table(box=box.HEAVY_EDGE, header_style="bold cyan", show_footer=True)
    results.add_column("Token",    style="bold white",  footer="[bold]TOTAL[/bold]")
    results.add_column("Side",                          footer="")
    results.add_column("Entry",    justify="right",     footer="")
    results.add_column("Exit",     justify="right",     footer="")
    results.add_column("Move",     justify="right",     footer="")
    results.add_column("P&L $",    justify="right",     footer="")
    results.add_column("P&L %",    justify="right",     footer="")
    results.add_column("Result",                        footer="")

    total_pnl  = 0.0
    total_cost = 0.0
    wins = 0
    losses = 0

    for t in trades:
        pnl   = t.realised_pnl if t.closed else t.unrealised_pnl
        pct   = t.realised_pct if t.closed else t.unrealised_pct
        total_pnl  += pnl
        total_cost += t.cost_usd

        if t.closed:
            if t.exit_reason == "target":
                result_text = Text("✓  TARGET HIT",  style="bold green")
                wins += 1
            else:
                result_text = Text("✗  STOPPED OUT", style="bold red")
                losses += 1
        else:
            result_text = Text("●  STILL OPEN",  style="bold cyan")

        exit_disp = f"${t.exit_price:.4f}" if t.closed else f"${t.current_price:.4f}*"
        move_disp = Text(f"{pct:+.2f}%", style="green" if pct >= 0 else "red")
        pnl_disp  = Text(f"{'+'if pnl>=0 else ''}{pnl:.2f}", style=_pnl_color(pnl))

        results.add_row(
            t.token, "BUY",
            f"${t.entry_price:.4f}",
            exit_disp,
            move_disp, pnl_disp,
            Text(f"{pct:+.2f}%", style=_pnl_color(pct)),
            result_text,
        )

    # Footer totals
    total_pct = total_pnl / total_cost * 100 if total_cost > 0 else 0
    results.columns[5].footer = Text(
        f"{'+'if total_pnl>=0 else ''}{total_pnl:.2f}",
        style="bold green" if total_pnl >= 0 else "bold red",
    )
    results.columns[6].footer = Text(
        f"{total_pct:+.2f}%",
        style="bold green" if total_pct >= 0 else "bold red",
    )
    console.print(results)

    # ── Run summary ────────────────────────────────────────────────────────────
    console.print()
    win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    summary_style = "green" if total_pnl >= 0 else "red"
    console.print(Panel(
        f"  Trades closed: [bold]{wins + losses}[/bold]"
        f"  ·  Wins: [green]{wins}[/green]"
        f"  ·  Losses: [red]{losses}[/red]"
        f"  ·  Win rate: [bold]{win_rate:.0f}%[/bold]\n"
        f"  Net P&L: [{summary_style}][bold]"
        f"{'+'if total_pnl>=0 else ''}{total_pnl:.2f} USD"
        f"  ({total_pct:+.2f}% on deployed capital)[/bold][/{summary_style}]",
        title="[bold white]Run Summary[/bold white]",
        border_style=summary_style,
        width=70,
    ))
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="WINS paper trade simulator")
    parser.add_argument("--synthetic", action="store_true",
                        help="Use built-in scenarios instead of live prices")
    parser.add_argument("--ticks",  type=int, default=10,
                        help="Number of price ticks to simulate (default: 10)")
    parser.add_argument("--speed",  choices=["fast", "normal", "slow"], default="normal",
                        help="Tick animation speed (default: normal)")
    args = parser.parse_args()

    asyncio.run(run(synthetic=args.synthetic, ticks=args.ticks, speed=args.speed))


if __name__ == "__main__":
    main()
