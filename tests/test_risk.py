"""
tests/test_risk.py
Hard-rule regression tests for the risk layer.
Every rule that can block a trade must have a test here — a failure in this
layer cannot be tolerated in production.

Run with: python -m pytest tests/test_risk.py -v
"""
import pytest
from decimal import Decimal

from wins.shared.models import DecisionOutput, Action, SignalType, TimeHorizon, MacroGate, RiskFlag
from wins.execution.risk import validate_decision


def _buy(
    *,
    confidence: str = "0.75",
    entry: str = "100.00",
    stop_loss: str = "85.00",    # 15% below entry — within 20% cap
    target: str = "130.00",      # 30% above entry → 2:1 R:R
    signal_type: SignalType = SignalType.momentum,
    macro_gate: MacroGate = MacroGate.pass_gate,
    risk_flag: RiskFlag = RiskFlag.none,
) -> DecisionOutput:
    return DecisionOutput(
        action             = Action.buy,
        token              = "SOL",
        confidence         = Decimal(confidence),
        signal_type        = signal_type,
        entry_price        = Decimal(entry),
        stop_loss_price    = Decimal(stop_loss),
        target_price       = Decimal(target),
        estimated_move_pct = 20,
        time_horizon       = TimeHorizon.days,
        reasoning          = "test",
        macro_gate         = macro_gate,
        risk_flag          = risk_flag,
    )


def _hold(**kwargs) -> DecisionOutput:
    d = _buy(**kwargs)
    return d.model_copy(update={"action": Action.hold})


CAPITAL      = Decimal("1000.00")
OPEN_POS     = 0
STARTING_CAP = Decimal("1000.00")


# ─── Rule 1: Macro gate blocks ────────────────────────────────────────────────

def test_macro_gate_blocks():
    d = _buy(macro_gate=MacroGate.block)
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert not ok
    assert "Macro gate" in reason


# ─── Rule 2: Hold always passes (no execution) ───────────────────────────────

def test_hold_passes():
    d = _hold(macro_gate=MacroGate.block)  # even with macro block, hold passes
    ok, _ = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert ok


# ─── Rule 3: Minimum confidence ──────────────────────────────────────────────

def test_low_confidence_blocked():
    d = _buy(confidence="0.60")
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert not ok
    assert "Confidence" in reason


def test_exact_min_confidence_passes():
    d = _buy(confidence="0.65")
    ok, _ = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert ok


# ─── Rule 4: Max open positions ──────────────────────────────────────────────

def test_max_open_positions_blocked():
    d = _buy()
    ok, reason = validate_decision(d, CAPITAL, open_positions=2, starting_capital_usd=STARTING_CAP)
    assert not ok
    assert "Max open positions" in reason


# ─── Rule 5: Stop loss > 20% blocked ─────────────────────────────────────────

def test_sl_too_wide_blocked():
    # 25% stop — exceeds the 20% hard cap
    d = _buy(entry="100.00", stop_loss="74.00", target="160.00")
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert not ok
    assert "Stop loss distance" in reason


def test_sl_zero_blocked():
    d = _buy(stop_loss="0.00")
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert not ok
    assert "Stop loss price must be > 0" in reason


# ─── Rule 5b: R:R < 2:1 blocked ──────────────────────────────────────────────

def test_rr_below_2_blocked():
    # 10% SL, 15% TP → 1.5:1 R:R — below minimum 2:1
    d = _buy(entry="100.00", stop_loss="90.00", target="115.00")
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert not ok
    assert "R:R" in reason


def test_rr_exactly_2_passes():
    # 10% SL, 20% TP → exactly 2:1
    d = _buy(entry="100.00", stop_loss="90.00", target="120.00")
    ok, _ = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert ok


def test_rr_above_2_passes():
    d = _buy(entry="100.00", stop_loss="85.00", target="130.00")
    ok, _ = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert ok


# ─── Rule 7: Drawdown kill switch ────────────────────────────────────────────

def test_kill_switch_fires_at_40pct():
    # Started at 1000, now at 590 liquid, no open positions → 41% realized loss
    ok, reason = validate_decision(_buy(), Decimal("590.00"), OPEN_POS, STARTING_CAP)
    assert not ok
    assert "KILL SWITCH" in reason


def test_kill_switch_does_not_fire_below_40pct():
    # 39% realized drawdown — should still allow trades
    ok, _ = validate_decision(_buy(), Decimal("611.00"), OPEN_POS, STARTING_CAP)
    assert ok


def test_kill_switch_uses_starting_capital_not_current():
    # Starting cap 1000, current 700 → 30% drawdown — should pass
    ok, _ = validate_decision(
        _buy(),
        capital_usd=Decimal("700.00"),
        open_positions=0,
        starting_capital_usd=Decimal("1000.00"),
    )
    assert ok


def test_kill_switch_ignores_committed_position_capital():
    # Started at 100, 50 liquid + 50 committed in open position = no realized loss
    ok, _ = validate_decision(
        _buy(),
        capital_usd=Decimal("50.00"),
        open_positions=1,
        starting_capital_usd=Decimal("100.00"),
        open_position_cost=Decimal("50.00"),
    )
    assert ok


def test_kill_switch_fires_on_realized_loss_with_open_positions():
    # Started at 1000, 150 liquid + 400 committed = 550 effective → 45% realized loss
    ok, reason = validate_decision(
        _buy(),
        capital_usd=Decimal("150.00"),
        open_positions=1,
        starting_capital_usd=Decimal("1000.00"),
        open_position_cost=Decimal("400.00"),
    )
    assert not ok
    assert "KILL SWITCH" in reason


# ─── Rule 8: risk_flag=high blocks ───────────────────────────────────────────

def test_risk_flag_high_blocked():
    d = _buy(risk_flag=RiskFlag.high)
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert not ok
    assert "risk_flag=high" in reason


def test_risk_flag_caution_passes():
    d = _buy(risk_flag=RiskFlag.caution)
    ok, _ = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert ok


# ─── Happy path — all checks pass ────────────────────────────────────────────

def test_valid_buy_passes_all():
    d = _buy()
    ok, reason = validate_decision(d, CAPITAL, OPEN_POS, STARTING_CAP)
    assert ok, f"Expected pass but got: {reason}"
