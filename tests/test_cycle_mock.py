"""
tests/test_cycle_mock.py
Smoke tests for the full mock pipeline.

Validates:
  1. make_decision returns the 5-tuple that cycle.py unpacks — interface regression guard
  2. A strong-momentum bundle triggers a buy and writes to trade_log
  3. A hold decision writes to decision_log but not trade_log
  4. A paused system skips collection entirely

Run with: python -m pytest tests/test_cycle_mock.py -v
"""
import asyncio
import contextlib
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from wins.shared.models import MarketSnapshot, SignalBundle


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _bundle(token: str = "SOL", change_pct: str = "10.0") -> SignalBundle:
    """Build a SignalBundle. change_pct >= 8 triggers mock buy, < 8 → hold."""
    return SignalBundle(
        token=token,
        market=MarketSnapshot(
            token=token,
            price_usd=Decimal("100.00"),
            volume_24h_usd=Decimal("50_000_000"),
            change_24h_pct=Decimal(change_pct),
        ),
        macro=MarketSnapshot(
            token="BTC",
            price_usd=Decimal("60000"),
            volume_24h_usd=Decimal("1_000_000_000"),
            change_24h_pct=Decimal("1.0"),  # healthy — no macro block
            btc_dominance=Decimal("50.0"),
        ),
    )


def _mock_pool() -> MagicMock:
    """
    Minimal asyncpg pool mock that satisfies all queries in run_cycle().
    Tracks execute() calls so tests can assert trade_log writes.
    """
    pool = MagicMock()

    state_row = {
        "id": 1,
        "run_number": 1,
        "phase": "paper",
        "capital_usd": Decimal("1000.00"),
        "run_starting_capital": Decimal("1000.00"),
        "trade_mode": "paper",
        "system_paused": False,
        "pause_reason": None,
        "open_positions": 0,
        "ts": None,
    }

    async def _fetchrow(sql, *args):
        if "system_state" in sql:
            return state_row
        return {"id": 1}  # INSERT INTO decision_log ... RETURNING id

    async def _fetch(sql, *args):
        return []  # no open positions

    execute_calls: list[tuple] = []

    async def _execute(sql, *args):
        execute_calls.append((sql, args))

    pool.fetchrow = _fetchrow
    pool.fetch = _fetch
    pool.execute = _execute
    pool._execute_calls = execute_calls
    return pool


_ALERT_NAMES = [
    "wins.brain.cycle.alert_trade_opened",
    "wins.brain.cycle.alert_trade_closed",
    "wins.brain.cycle.alert_kill_switch",
    "wins.brain.cycle.alert_system_health",
]


def _apply_cycle_patches(stack, mock_pool, bundles, collect_mock=None):
    """Push all standard run_cycle patches onto an ExitStack."""
    if collect_mock is None:
        collect_mock = AsyncMock(return_value=bundles)
    stack.enter_context(patch("wins.brain.cycle.collect_signal_bundles", new=collect_mock))
    stack.enter_context(patch("wins.brain.cycle.get_pool", new=AsyncMock(return_value=mock_pool)))
    stack.enter_context(patch("wins.brain.cycle.check_and_close_positions",
                              new=AsyncMock(return_value=[])))
    stack.enter_context(patch("wins.brain.decision.USE_MOCK_BRAIN", True))
    stack.enter_context(patch("wins.brain.cycle.write_status"))
    for name in _ALERT_NAMES:
        stack.enter_context(patch(name, new=AsyncMock()))
    return collect_mock


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_make_decision_returns_5tuple():
    """make_decision must return a 5-tuple — interface regression guard."""
    from wins.brain.decision import make_decision

    with patch("wins.brain.decision.USE_MOCK_BRAIN", True):
        result = make_decision(
            _bundle(change_pct="10.0"),
            account_state={"capital_usd": 1000, "open_positions": 0},
            as_of="2026-01-01T00:00:00Z",
        )

    assert len(result) == 5, f"Expected 5-tuple, got {len(result)}-tuple"
    decision, model_used, input_tokens, output_tokens, cache_read_tokens = result
    assert decision is not None
    assert isinstance(model_used, str)
    assert isinstance(input_tokens, int)
    assert isinstance(output_tokens, int)
    assert isinstance(cache_read_tokens, int)


def test_mock_cycle_buy_writes_trade_log():
    """
    Strong-momentum bundle (10% up) → mock brain buys →
    risk layer approves → trade_log INSERT fires.
    """
    mock_pool = _mock_pool()

    async def _run():
        with contextlib.ExitStack() as stack:
            _apply_cycle_patches(stack, mock_pool, [_bundle(change_pct="10.0")])
            from wins.brain import cycle as cycle_mod
            await cycle_mod.run_cycle()

    asyncio.run(_run())

    trade_inserts = [sql for sql, _ in mock_pool._execute_calls if "trade_log" in sql]
    assert trade_inserts, "Expected INSERT into trade_log for a buy decision"


def test_mock_cycle_hold_no_trade_log():
    """
    Weak-momentum bundle (2% up) → mock brain holds → no trade_log INSERT.
    """
    mock_pool = _mock_pool()

    async def _run():
        with contextlib.ExitStack() as stack:
            _apply_cycle_patches(stack, mock_pool, [_bundle(change_pct="2.0")])
            from wins.brain import cycle as cycle_mod
            await cycle_mod.run_cycle()

    asyncio.run(_run())

    trade_inserts = [sql for sql, _ in mock_pool._execute_calls if "trade_log" in sql]
    assert not trade_inserts, "Expected no INSERT into trade_log for a hold decision"


def test_mock_cycle_paused_system_skips():
    """Paused system_state → run_cycle returns early, collect never called."""
    mock_pool = _mock_pool()

    paused_row = {
        "id": 1, "run_number": 1, "phase": "paper",
        "capital_usd": Decimal("600.00"),
        "run_starting_capital": Decimal("1000.00"),
        "trade_mode": "paper",
        "system_paused": True,
        "pause_reason": "KILL SWITCH: test",
        "open_positions": 0, "ts": None,
    }

    async def _fetchrow_paused(sql, *args):
        return paused_row

    mock_pool.fetchrow = _fetchrow_paused

    collect_mock = AsyncMock(return_value=[_bundle()])

    async def _run():
        with contextlib.ExitStack() as stack:
            _apply_cycle_patches(stack, mock_pool, [], collect_mock=collect_mock)
            from wins.brain import cycle as cycle_mod
            await cycle_mod.run_cycle()

    asyncio.run(_run())

    collect_mock.assert_not_called()
