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
    TRADE_MODE, MAX_SINGLE_POSITION_PCT, DRAWDOWN_KILL_SWITCH, LUNARCRUSH_API_KEY,
)
from wins.shared.db import get_pool
from wins.shared.logger import get_logger
from wins.shared.models import Action, MacroGate
from wins.ingestion.collector import collect_signal_bundles, apply_social_filter
from wins.brain.calibration import get_calibration_multipliers
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


async def _log_social_signals(pool: asyncpg.Pool, bundles: list) -> None:
    """Write raw LunarCrush fields to signal_log for backtest replay."""
    import json
    rows = [
        (b.token, "sentiment", json.dumps(b.social_raw), b.social_summary)
        for b in bundles
        if b.social_raw
    ]
    if rows:
        await pool.executemany(
            "INSERT INTO signal_log (token, signal_type, raw_data, summary) "
            "VALUES ($1, $2, $3::jsonb, $4)",
            rows,
        )


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
    # Load every open position once — used for cost, held_tokens, and as
    # context for Claude so it can reason about discretionary exits.
    open_position_rows = await pool.fetch(
        """SELECT id, token, qty, entry_price, stop_loss_price, target_price,
                  ts_open, exchange_order_id
             FROM trade_log
            WHERE ts_close IS NULL AND side = 'buy'"""
    )
    open_position_cost = sum(
        (Decimal(str(r["qty"])) * Decimal(str(r["entry_price"])) for r in open_position_rows),
        Decimal("0"),
    )
    held_tokens = {r["token"] for r in open_position_rows}
    open_positions_by_token = {r["token"]: r for r in open_position_rows}

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    calibration_multipliers = await get_calibration_multipliers(pool)
    if calibration_multipliers:
        log.info(f"Calibration multipliers active: {dict(calibration_multipliers)}")

    # Collect signals
    write_status("ingesting")
    bundles = await collect_signal_bundles()
    if not bundles:
        log.warning("No signal bundles returned — skipping cycle.")
        return
    await _log_social_signals(pool, bundles)
    await apply_social_filter(pool, bundles)

    # Always available — used both for paper SL/TP checks and for marking
    # discretionary sells to market.
    current_prices = {b.token: b.market.price_usd for b in bundles}

    # ── Step 1: Check open paper positions for SL/TP hits ─────────────────────
    if TRADE_MODE == "paper":
        closed = await check_and_close_positions(pool, current_prices)
        for c in closed:
            capital        += Decimal(str(c["cost_usd"])) + Decimal(str(c["pnl_usd"]))
            open_positions  = max(0, open_positions - 1)
            held_tokens.discard(c["token"])
            await alert_trade_closed(c["token"], c["pnl_usd"], c["pnl_pct"], c["exit_reason"], TRADE_MODE)

    executor = get_executor()

    # ── Step 2a: Collect a decision per bundle (no execution yet) ─────────────
    # Two-pass design: gather all decisions first, then execute sells before buys
    # and buys in confidence order. Cash-based position sizing means the first
    # buy executed gets the largest slice, so highest-confidence wins it.
    decisions: list[tuple] = []   # (decision, bundle, decision_id)
    for bundle in bundles:
        positions_detail = []
        for token, r in open_positions_by_token.items():
            entry = Decimal(str(r["entry_price"]))
            qty   = Decimal(str(r["qty"]))
            cur   = current_prices.get(token)
            detail = {
                "token":           token,
                "qty":             float(qty),
                "entry_price":     float(entry),
                "stop_loss_price": float(r["stop_loss_price"]),
                "target_price":    float(r["target_price"]),
                "ts_open":         r["ts_open"].isoformat(),
            }
            if cur is not None and entry > 0:
                detail["current_price"]      = float(cur)
                detail["unrealized_pnl_pct"] = float((Decimal(str(cur)) - entry) / entry * 100)
            positions_detail.append(detail)

        account_state = {
            "capital_usd":           float(capital),
            "open_positions":        open_positions,
            "open_positions_detail": positions_detail,
        }

        if LUNARCRUSH_API_KEY and not bundle.social_data_ok:
            log.warning(
                f"Skipping Claude for {bundle.token}: LunarCrush fetch failed "
                f"(API key is set). Holding until data recovers."
            )
            continue

        if bundle.social_filter_verdict == "skip":
            log.info(f"Social filter suppressed Claude call for {bundle.token} — holding.")
            continue

        decision, model_used, input_tokens, output_tokens, cache_read_tokens = make_decision(
            bundle, account_state=account_state, as_of=as_of,
        )
        if decision is None:
            continue

        decision_id = await _log_decision(
            pool, decision, bundle, model_used,
            input_tokens, output_tokens, cache_read_tokens,
        )

        if decision.action != Action.hold:
            decisions.append((decision, bundle, decision_id))

    # ── Step 2b: Execute sells first to free up capital ───────────────────────
    for decision, bundle, decision_id in [d for d in decisions if d[0].action == Action.sell]:
        approved, reason = validate_decision(
            decision, capital, open_positions, starting_cap, open_position_cost,
            calibration_multipliers=calibration_multipliers or None,
            held_tokens=held_tokens,
        )
        if not approved:
            log.info(f"Sell blocked for {bundle.token}: {reason}")
            if "KILL SWITCH" in reason:
                await pool.execute(
                    "UPDATE system_state SET system_paused=TRUE, pause_reason=$1 "
                    "WHERE id=(SELECT MAX(id) FROM system_state)",
                    reason,
                )
                await alert_kill_switch(reason)
            continue

        pos_row = open_positions_by_token.get(bundle.token)
        if pos_row is None:
            log.warning(f"Claude returned sell for unheld {bundle.token} — ignoring.")
            continue

        cur_price = current_prices.get(bundle.token)
        if cur_price is None:
            log.warning(f"No current price for {bundle.token} — skipping discretionary sell.")
            continue

        qty         = Decimal(str(pos_row["qty"]))
        entry_price = Decimal(str(pos_row["entry_price"]))
        cost_usd    = qty * entry_price
        cur_dec     = Decimal(str(cur_price))
        pnl_usd     = (cur_dec - entry_price) * qty
        pnl_pct     = ((cur_dec - entry_price) / entry_price) * Decimal("100") if entry_price > 0 else Decimal("0")

        sell_kwargs = {"token": bundle.token, "qty": qty, "current_price": cur_dec, "reason": "claude_sell"}
        if TRADE_MODE == "live":
            sell_kwargs["sl_order_id"] = pos_row["exchange_order_id"]
        fill = await executor.sell(**sell_kwargs)

        sell_notes = (
            f"signal_type={decision.signal_type.value} "
            f"confidence={decision.confidence} "
            f"sell_decision_id={decision_id} "
            f"| {decision.reasoning}"
        )
        await pool.execute(
            """UPDATE trade_log
                  SET ts_close    = NOW(),
                      exit_price  = $1,
                      pnl_usd     = $2,
                      pnl_pct     = $3,
                      exit_reason = $4,
                      notes       = $5
                WHERE id = $6""",
            float(fill["fill_price"]),
            float(pnl_usd),
            float(pnl_pct),
            f"claude_sell:{decision.signal_type.value}",
            sell_notes,
            pos_row["id"],
        )
        log.info(
            f"[CLAUDE SELL] {bundle.token}: signal={decision.signal_type.value} "
            f"confidence={decision.confidence} pnl=${pnl_usd:.2f} ({pnl_pct:.2f}%) "
            f"| {decision.reasoning[:200]}"
        )

        capital            += cost_usd + pnl_usd
        open_positions      = max(0, open_positions - 1)
        open_position_cost  = max(Decimal("0"), open_position_cost - cost_usd)
        held_tokens.discard(bundle.token)
        open_positions_by_token.pop(bundle.token, None)

        await _persist_state(pool, capital, open_positions)
        await alert_trade_closed(
            bundle.token, float(pnl_usd), float(pnl_pct), "claude_sell", TRADE_MODE,
        )

    # ── Step 2c: Execute buys, highest confidence first ───────────────────────
    # Cash-based sizing means each successive buy is half the prior cash, so
    # ordering by confidence desc puts the strongest conviction in the largest slice.
    buy_candidates = sorted(
        [d for d in decisions if d[0].action == Action.buy],
        key=lambda d: d[0].confidence,
        reverse=True,
    )
    for decision, bundle, decision_id in buy_candidates:
        approved, reason = validate_decision(
            decision, capital, open_positions, starting_cap, open_position_cost,
            calibration_multipliers=calibration_multipliers or None,
            held_tokens=held_tokens,
        )
        if not approved:
            log.info(f"Buy blocked for {bundle.token}: {reason}")
            if "KILL SWITCH" in reason:
                await pool.execute(
                    "UPDATE system_state SET system_paused=TRUE, pause_reason=$1 "
                    "WHERE id=(SELECT MAX(id) FROM system_state)",
                    reason,
                )
                await alert_kill_switch(reason)
            continue

        write_status("trading")
        position_usd = calculate_position_size(capital, decision.entry_price)
        fill = await executor.buy(decision, position_usd)

        sl_order_id = fill.get("sl_order_id")

        await pool.execute(
            """INSERT INTO trade_log
                 (decision_id, token, trade_mode, side, qty, entry_price,
                  stop_loss_price, target_price, exchange_order_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            decision_id,
            fill["token"], TRADE_MODE, "buy",
            Decimal(str(fill["qty"])), Decimal(str(fill["fill_price"])),
            decision.stop_loss_price, decision.target_price,
            sl_order_id,
        )

        open_positions     += 1
        capital            -= position_usd
        open_position_cost += position_usd
        held_tokens.add(fill["token"])

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
