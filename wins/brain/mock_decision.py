"""
wins/brain/mock_decision.py
Rule-based decision engine for testing with no Anthropic API key.

Implements the same logic hierarchy as the real brain prompt:
  1. Macro gate (BTC 24h change, dominance)
  2. Momentum signal (price/volume)
  3. Conservative hold bias

Produces a valid DecisionOutput so the entire pipeline — risk layer,
paper executor, DB logging, Telegram alerts — can be exercised end-to-end
without any API spend.
"""
from decimal import Decimal

from wins.shared.models import (
    DecisionOutput, SignalBundle,
    Action, SignalType, TimeHorizon, MacroGate, RiskFlag,
)
from wins.shared.logger import get_logger

log = get_logger("brain.mock")

# Thresholds used by mock rules
_BTC_FREEFALL_PCT   = Decimal("-5.0")   # BTC drop that triggers macro block
_BTC_DOMINANCE_RISE = Decimal("0.5")    # dominance delta considered "rising sharply" (placeholder)
_STRONG_PUMP_PCT    = Decimal("0.5")    # TEMP: lowered for testing (restore to 8.0)
_STRONG_DUMP_PCT    = Decimal("-8.0")   # 24h loss that triggers caution/sell signal
_MIN_VOLUME_USD     = Decimal("10_000_000")   # skip illiquid tokens


def mock_decision(bundle: SignalBundle) -> DecisionOutput:
    """
    Deterministic, rule-based decision — no external calls.
    Always returns a valid DecisionOutput.
    """
    token  = bundle.token
    price  = bundle.market.price_usd
    change = bundle.market.change_24h_pct
    volume = bundle.market.volume_24h_usd
    btc_change = bundle.macro.change_24h_pct

    # ── 1. Macro gate ──────────────────────────────────────────────────────────
    if btc_change <= _BTC_FREEFALL_PCT:
        log.info(f"[MOCK] Macro block on {token}: BTC change={btc_change}%")
        return DecisionOutput(
            action             = Action.hold,
            token              = token,
            confidence         = Decimal("0.30"),
            signal_type        = SignalType.macro,
            entry_price        = price,
            stop_loss_price    = price * Decimal("0.85"),
            target_price       = price,
            estimated_move_pct = 0,
            time_horizon       = TimeHorizon.hours,
            reasoning          = f"BTC in freefall ({btc_change:.1f}% 24h). Macro gate blocked. No new positions.",
            macro_gate         = MacroGate.block,
            risk_flag          = RiskFlag.high,
        )

    # ── 2. Liquidity guard ────────────────────────────────────────────────────
    if volume < _MIN_VOLUME_USD:
        log.info(f"[MOCK] Low volume hold on {token}: volume=${volume}")
        return _hold(token, price, "Insufficient 24h volume for safe entry.", MacroGate.pass_gate, RiskFlag.caution)

    # ── 3. Strong upside momentum — buy signal ────────────────────────────────
    if change >= _STRONG_PUMP_PCT:
        sl    = price * Decimal("0.88")   # 12% stop (within 20% hard cap)
        tp    = price * Decimal("1.25")   # 25% target → ~2.1:1 R:R
        est   = 25
        log.info(f"[MOCK] Momentum BUY on {token}: change={change:.1f}%")
        return DecisionOutput(
            action             = Action.buy,
            token              = token,
            confidence         = Decimal("0.72"),
            signal_type        = SignalType.momentum,
            entry_price        = price,
            stop_loss_price    = sl,
            target_price       = tp,
            estimated_move_pct = est,
            time_horizon       = TimeHorizon.days,
            reasoning          = (
                f"Strong 24h momentum: {change:.1f}%. "
                f"BTC healthy at {btc_change:.1f}%. "
                f"Entry ${price}, SL ${sl:.4f}, TP ${tp:.4f}."
            ),
            macro_gate         = MacroGate.pass_gate,
            risk_flag          = RiskFlag.none,
        )

    # ── 4. Strong downside — raise caution, hold ─────────────────────────────
    if change <= _STRONG_DUMP_PCT:
        log.info(f"[MOCK] Downside caution on {token}: change={change:.1f}%")
        return _hold(
            token, price,
            f"Token down {change:.1f}% in 24h. Waiting for stabilisation before entry.",
            MacroGate.pass_gate, RiskFlag.caution,
        )

    # ── 5. Default — hold, nothing actionable ────────────────────────────────
    log.info(f"[MOCK] Hold on {token}: no strong signal (change={change:.1f}%)")
    return _hold(
        token, price,
        f"No high-conviction signal. 24h change {change:.1f}%. Staying in cash.",
        MacroGate.pass_gate, RiskFlag.none,
    )


def _hold(
    token: str,
    price: Decimal,
    reasoning: str,
    macro_gate: MacroGate,
    risk_flag: RiskFlag,
) -> DecisionOutput:
    return DecisionOutput(
        action             = Action.hold,
        token              = token,
        confidence         = Decimal("0.50"),
        signal_type        = SignalType.momentum,
        entry_price        = price,
        stop_loss_price    = price * Decimal("0.85"),
        target_price       = price,
        estimated_move_pct = 0,
        time_horizon       = TimeHorizon.hours,
        reasoning          = reasoning,
        macro_gate         = macro_gate,
        risk_flag          = risk_flag,
    )
