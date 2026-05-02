"""
wins/brain/cycle.py
Main decision cycle — runs every DECISION_INTERVAL_MINUTES.
Orchestrates: ingestion → brain → risk → execution → logging → alerts.
"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

from wins.shared.config import (
    TRADE_MODE, MAX_SINGLE_POSITION_PCT, DRAWDOWN_KILL_SWITCH,
)
from wins.shared.db import get_pool
from wins.shared.logger import get_logger
from wins.shared.models import Action, MacroGate
from wins.ingestion.collector import collect_signal_bundles
from wins.brain.decision import make_decision
from wins.execution.risk import validate_decision, calculate_position_size
from wins.execution.executor import get_executor
from wins.execution.paper_portfolio import check_and_close_positions
from wins.alerts.discord_bot import (
    alert_trade_opened, alert_trade_closed, alert_kill_switch, alert_system_health,
)
from wins.alerts.presence import write_status

log = get_logger("brain.cycle")


async def _get_system_state(pool: asyncpg.Pool) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM system_state ORDER BY ts DESC LIMIT 1"
    )
    if not row:
        initial_capital = Decimal("100.00")
        await pool.execute(
            """INSERT INTO system_state
                 (run_number, phase, capital_usd, run_starting_capital,
                  trade_mode, system_paused, open_positions)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            1, "paper", initial_capital, initial_capital, TRADE_MODE, False, 0,
        )
        return await _get_system_state(pool)
    state = dict(row)
    # Back-fill run_starting_capital for existing rows that predate the column
    if state.get("run_starting_capital") is None:
        await pool.execute(
            "UPDATE system_state SET run_starting_capital = capital_usd "
            "WHERE id = $1",
            state["id"],
        )
        state["run_starting_capital"] = state["capital_usd"]
    return state


async def _log_decision(
    pool: asyncpg.Pool,
    decision,
    bundle,
    model_used: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO decision_log
             (token, action, confidence, signal_type, entry_price, stop_loss_price,
              target_price, estimated_move_pct, time_horizon, reasoning,
              macro_gate, risk_flag, raw_response, model_used,
              prompt_tokens, completion_tokens, cache_read_tokens)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
           RETURNING id""",
        decision.token, decision.action.value, float(decision.confidence),
        decision.signal_type.value, float(decision.entry_price),
        float(decision.stop_loss_price), float(decision.target_price),
        decision.estimated_move_pct, decision.time_horizon.value,
        decision.reasoning, decision.macro_gate.value, decision.risk_flag.value,
        json.dumps(decision.model_dump(mode="json")), model_used,
        input_tokens, output_tokens, cache_read_tokens,
    )
    return row["id"]


async def _persist_state(
    pool: asyncpg.Pool,
    capital: Decimal,
    open_positions: int,
) -> None:
    await pool.execute(
        "UPDATE system_state SET capital_usd=$1, open_positions=$2, ts=NOW() "
        "WHERE id=(SELECT MAX(id) FROM system_state)",
        capital, open_positions,
    )


async def run_cycle() -> None:
    pool = await get_pool()
    state = await _get_system_state(pool)

    if state["system_paused"]:
        log.warning(f"System is PAUSED: {state.get('pause_reason')}. Skipping cycle.")
        return

    capital            = Decimal(str(state["capital_usd"]))
    open_positions     = state["open_positions"]
    # Use the run's starting capital for drawdown calc (not current — fixes kill-switch bug)
    starting_cap       = Decimal(str(state["run_starting_capital"]))
    # Sum of (qty × entry_price) for all open positions — excluded from drawdown calc
    open_position_cost = Decimal(str(await pool.fetchval(
        "SELECT COALESCE(SUM(qty * entry_price), 0) FROM trade_log WHERE ts_close IS NULL AND side = 'buy'"
    )))

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Collect signals
    write_status("ingesting")
    bundles = await collect_signal_bundles()
    if not bundles:
        log.warning("No signal bundles returned — skipping cycle.")
        return

    # ── Step 1: Check open paper positions for SL/TP hits ─────────────────────
    if TRADE_MODE == "paper":
        current_prices = {b.token: b.market.price_usd for b in bundles}
        closed = await check_and_close_positions(pool, current_prices)
        for c in closed:
            capital        += Decimal(str(c["cost_usd"])) + Decimal(str(c["pnl_usd"]))
            open_positions  = max(0, open_positions - 1)
            await alert_trade_closed(c["token"], c["pnl_usd"], c["pnl_pct"], c["exit_reason"], TRADE_MODE)

    executor = get_executor()

    # ── Step 2: Evaluate new entries ──────────────────────────────────────────
    for bundle in bundles:
        account_state = {
            "capital_usd":    float(capital),
            "open_positions": open_positions,
        }

        decision, model_used, input_tokens, output_tokens, cache_read_tokens = make_decision(
            bundle, account_state=account_state, as_of=as_of,
        )
        if decision is None:
            continue

        # Log every decision regardless of approval
        await _log_decision(
            pool, decision, bundle, model_used,
            input_tokens, output_tokens, cache_read_tokens,
        )

        approved, reason = validate_decision(
            decision, capital, open_positions, starting_cap, open_position_cost
        )

        if not approved:
            log.info(f"Trade blocked for {bundle.token}: {reason}")
            if "KILL SWITCH" in reason:
                await pool.execute(
                    "UPDATE system_state SET system_paused=TRUE, pause_reason=$1 "
                    "WHERE id=(SELECT MAX(id) FROM system_state)",
                    reason,
                )
                await alert_kill_switch(reason)
            continue

        if decision.action == Action.hold:
            continue

        if decision.action == Action.buy:
            write_status("trading")
            position_usd = calculate_position_size(capital, decision.entry_price)
            fill = await executor.buy(decision, position_usd)

            sl_order_id = fill.get("sl_order_id")   # only set for live trades

            await pool.execute(
                """INSERT INTO trade_log
                     (decision_id, token, trade_mode, side, qty, entry_price,
                      stop_loss_price, target_price, exchange_order_id)
                   VALUES ((SELECT MAX(id) FROM decision_log), $1, $2, $3, $4, $5, $6, $7, $8)""",
                fill["token"], TRADE_MODE, "buy",
                Decimal(str(fill["qty"])), Decimal(str(fill["fill_price"])),
                decision.stop_loss_price, decision.target_price,
                sl_order_id,
            )

            open_positions     += 1
            capital            -= position_usd
            open_position_cost += position_usd

            # Persist immediately after each trade to protect against mid-cycle crash
            await _persist_state(pool, capital, open_positions)

            await alert_trade_opened(
                fill["token"], "buy", fill["fill_price"],
                float(decision.stop_loss_price), float(decision.target_price),
                float(position_usd), float(decision.confidence),
                decision.reasoning, TRADE_MODE,
            )

    # ── Step 3: Final state persist (no-op if nothing traded) ─────────────────
    await _persist_state(pool, capital, open_positions)

    write_status("idle")
    await alert_system_health(float(capital), open_positions, state["phase"], TRADE_MODE)
    log.info(f"Cycle complete. Capital=${capital:.2f} Open positions={open_positions}")
