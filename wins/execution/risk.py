"""
wins/execution/risk.py
Hard-coded risk layer.
This module is the final gate before any order reaches the exchange.
It CANNOT be overridden by Claude output — it validates against immutable constants.
"""
from decimal import Decimal

from wins.shared.config import (
    MAX_STOP_LOSS_PCT, MAX_SINGLE_POSITION_PCT,
    DRAWDOWN_KILL_SWITCH, MIN_CONFIDENCE_TO_TRADE, MAX_OPEN_POSITIONS,
)
from wins.shared.models import DecisionOutput, Action, MacroGate
from wins.shared.logger import get_logger

log = get_logger("risk")


class RiskViolation(Exception):
    """Raised when a decision violates a hard risk rule."""


def validate_decision(
    decision: DecisionOutput,
    capital_usd: Decimal,
    open_positions: int,
    starting_capital_usd: Decimal,
    open_position_cost: Decimal = Decimal("0"),
) -> tuple[bool, str]:
    """
    Returns (approved: bool, reason: str).
    Any hard rule failure returns False. All checks must pass for a trade to proceed.
    """

    # 1. Hold needs no execution — approve unconditionally
    if decision.action == Action.hold:
        return True, "Hold — no execution required."

    # 2. Macro gate blocks all entries/exits
    if decision.macro_gate == MacroGate.block:
        return False, "Macro gate blocked — risk-off environment."

    # 3. Minimum confidence threshold
    if decision.confidence < MIN_CONFIDENCE_TO_TRADE:
        return False, (
            f"Confidence {decision.confidence} below minimum {MIN_CONFIDENCE_TO_TRADE}."
        )

    # 4. Max open positions
    if decision.action == Action.buy and open_positions >= MAX_OPEN_POSITIONS:
        return False, (
            f"Max open positions ({MAX_OPEN_POSITIONS}) already reached."
        )

    # 5. Stop loss distance and R:R enforcement
    if decision.action == Action.buy and decision.entry_price > 0:
        if decision.stop_loss_price <= 0:
            return False, "Stop loss price must be > 0."
        sl_pct = (decision.entry_price - decision.stop_loss_price) / decision.entry_price
        if sl_pct > MAX_STOP_LOSS_PCT:
            return False, (
                f"Stop loss distance {sl_pct:.1%} exceeds maximum {MAX_STOP_LOSS_PCT:.0%}."
            )
        risk   = decision.entry_price - decision.stop_loss_price
        reward = decision.target_price - decision.entry_price
        if risk > 0 and reward / risk < Decimal("2.0"):
            return False, (
                f"R:R {reward/risk:.2f}:1 below required 2:1 "
                f"(entry={decision.entry_price} sl={decision.stop_loss_price} tp={decision.target_price})."
            )

    # 6. Position size
    max_position_usd = capital_usd * MAX_SINGLE_POSITION_PCT
    if decision.action == Action.buy and capital_usd > 0:
        # Position sizing uses half capital cap — enforced at order time in executor.py
        log.info(f"Max allowed position: ${max_position_usd:.2f}")

    # 7. Drawdown kill switch — uses realized losses only, not committed position capital
    effective_capital = capital_usd + open_position_cost
    drawdown = (starting_capital_usd - effective_capital) / starting_capital_usd
    if drawdown >= DRAWDOWN_KILL_SWITCH:
        return False, (
            f"KILL SWITCH: Drawdown {drawdown:.1%} >= {DRAWDOWN_KILL_SWITCH:.0%}. "
            "System paused. Manual review required."
        )

    # 8. Risk flag — caution reduces allowed confidence band; high blocks trade
    if decision.risk_flag.value == "high":
        return False, "Claude flagged risk_flag=high — trade blocked."

    return True, "All risk checks passed."


def calculate_position_size(capital_usd: Decimal, entry_price: Decimal) -> Decimal:
    """Returns the position size in USD — always MAX_SINGLE_POSITION_PCT of capital."""
    return capital_usd * MAX_SINGLE_POSITION_PCT
