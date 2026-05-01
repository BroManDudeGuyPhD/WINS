#!/usr/bin/env python
"""
scripts/run_cycle.py
One-shot decision cycle runner for local testing.
Runs the full pipeline — ingestion → brain → risk → paper execution —
and prints results without needing Docker or a live database.

Usage:
    # Mock brain (no API keys needed at all):
    USE_MOCK_BRAIN=true python scripts/run_cycle.py

    # Real Claude brain (needs ANTHROPIC_API_KEY):
    python scripts/run_cycle.py

    # Dry run — skip DB writes, just print decisions:
    python scripts/run_cycle.py --dry-run

    # Limit to specific tokens:
    python scripts/run_cycle.py --tokens SOL ARB LINK

Options:
    --dry-run       Print decisions but don't write to DB or send Discord alerts
    --tokens A B C  Only evaluate these tokens (faster for quick tests)
    --verbose       Show full reasoning + prices per decision
"""
import asyncio
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TRADE_MODE", "paper")
os.environ.setdefault("USE_MOCK_BRAIN", "true")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text
from rich.panel import Panel
from rich.rule import Rule

console = Console()


def _action_style(action: str) -> str:
    return {"buy": "bold green", "sell": "bold red", "hold": "dim"}.get(action, "white")


def _gate_style(gate: str) -> str:
    return "green" if gate == "pass" else "red"


def _risk_style(risk: str) -> str:
    return {"none": "green", "caution": "yellow", "high": "red"}.get(risk, "white")


def _render_decisions(decisions: list[dict], verbose: bool) -> None:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("Token",      style="bold white", width=8)
    table.add_column("Action",     width=6)
    table.add_column("Conf",       justify="right", width=5)
    table.add_column("Gate",       width=7)
    table.add_column("Risk",       width=7)
    table.add_column("Status",     width=11)
    table.add_column("Reason",     no_wrap=False)

    for d in decisions:
        action_text  = Text(d["action"].upper(), style=_action_style(d["action"]))
        gate_text    = Text(d["gate"],   style=_gate_style(d["gate"]))
        risk_text    = Text(d["risk"],   style=_risk_style(d["risk"]))
        status_text  = Text("✓ approved" if d["approved"] else "✗ blocked",
                            style="green" if d["approved"] else "red")
        table.add_row(
            d["token"], action_text,
            f"{d['conf']:.2f}", gate_text, risk_text,
            status_text, d["reason"],
        )
        if verbose and d["action"] == "buy":
            table.add_row(
                "", "", "", "", "", "",
                f"[dim]entry=${d['entry']:.4f}  SL=${d['sl']:.4f}  TP=${d['tp']:.4f}  "
                f"reasoning: {d['reasoning']}[/dim]",
            )

    console.print(table)


async def run(dry_run: bool, tokens: list[str] | None, verbose: bool) -> None:
    from wins.ingestion.collector import collect_signal_bundles
    from wins.brain.decision import make_decision
    from wins.execution.risk import validate_decision, calculate_position_size
    from wins.shared.config import USE_MOCK_BRAIN, TRADE_MODE
    from decimal import Decimal

    brain_label = "[yellow]MOCK[/yellow]" if USE_MOCK_BRAIN else "[cyan]CLAUDE[/cyan]"
    mode_label  = "[magenta]PAPER[/magenta]" if TRADE_MODE == "paper" else "[bold red]LIVE[/bold red]"
    dry_label   = " [dim](dry-run)[/dim]" if dry_run else ""

    console.print()
    console.print(Panel(
        f"  Brain: {brain_label}   Mode: {mode_label}{dry_label}",
        title="[bold white]WINS Cycle Test[/bold white]",
        border_style="cyan",
        width=60,
    ))

    with console.status("[cyan]Fetching market data...[/cyan]", spinner="dots"):
        bundles = await collect_signal_bundles()

    if not bundles:
        console.print("[bold red]ERROR:[/bold red] No bundles returned. Check CoinGecko connectivity.")
        return

    if tokens:
        upper   = [t.upper() for t in tokens]
        bundles = [b for b in bundles if b.token in upper]
        if not bundles:
            console.print(f"[bold red]ERROR:[/bold red] None of {upper} returned data.")
            return

    console.print(f"  [dim]Fetched {len(bundles)} token(s) — BTC macro: "
                  f"{bundles[0].macro.change_24h_pct:+.2f}% 24h[/dim]\n")

    capital        = Decimal("100.00")
    open_positions = 0
    rows: list[dict] = []
    buys:  list[dict] = []

    for bundle in bundles:
        decision = make_decision(bundle)
        if decision is None:
            console.print(f"[red]Brain returned None for {bundle.token}[/red]")
            continue

        approved, reason = validate_decision(decision, capital, open_positions, capital)

        rows.append({
            "token":    bundle.token,
            "action":   decision.action.value,
            "conf":     float(decision.confidence),
            "gate":     decision.macro_gate.value,
            "risk":     decision.risk_flag.value,
            "approved": approved,
            "reason":   reason,
            "entry":    float(decision.entry_price),
            "sl":       float(decision.stop_loss_price),
            "tp":       float(decision.target_price),
            "reasoning":decision.reasoning,
        })

        if approved and decision.action.value == "buy":
            pos_usd         = calculate_position_size(capital, decision.entry_price)
            capital        -= pos_usd
            open_positions += 1
            rr = (float(decision.target_price) - float(decision.entry_price)) / max(
                float(decision.entry_price) - float(decision.stop_loss_price), 0.0001
            )
            buys.append({
                "token": bundle.token,
                "entry": float(decision.entry_price),
                "sl":    float(decision.stop_loss_price),
                "tp":    float(decision.target_price),
                "size":  float(pos_usd),
                "rr":    rr,
            })

    _render_decisions(rows, verbose)

    # ── Summary ────────────────────────────────────────────────────────────────
    console.print(Rule(style="dim"))
    console.print(
        f"  Buy signals: [green]{len(buys)}[/green]   "
        f"Remaining capital: [yellow]${float(capital):.2f}[/yellow]"
    )

    if buys:
        console.print()
        pos_table = Table(title="Open Positions (simulated)", box=box.ROUNDED,
                          header_style="bold green")
        pos_table.add_column("Token",  style="bold white")
        pos_table.add_column("Entry",  justify="right")
        pos_table.add_column("SL",     justify="right", style="red")
        pos_table.add_column("Target", justify="right", style="green")
        pos_table.add_column("R:R",    justify="right")
        pos_table.add_column("Size",   justify="right", style="yellow")

        for b in buys:
            rr_color = "green" if b["rr"] >= 2 else "yellow"
            pos_table.add_row(
                b["token"],
                f"${b['entry']:.4f}",
                f"${b['sl']:.4f}",
                f"${b['tp']:.4f}",
                Text(f"{b['rr']:.1f}x", style=rr_color),
                f"${b['size']:.2f}",
            )
        console.print(pos_table)

    if dry_run:
        console.print("\n  [dim]Dry run — no DB writes or Discord alerts sent.[/dim]")
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="WINS one-shot cycle test")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--tokens",   nargs="+")
    parser.add_argument("--verbose",  action="store_true")
    args = parser.parse_args()

    asyncio.run(run(dry_run=args.dry_run, tokens=args.tokens, verbose=args.verbose))


if __name__ == "__main__":
    main()
