"""
wins/execution/executor.py
Order execution layer.
In TRADE_MODE=paper:  simulates fills, uses paper_portfolio for SL/TP tracking.
In TRADE_MODE=live:   submits real orders via ExchangeClient; stop-loss placed immediately.

Stop losses are placed as exchange-native orders immediately on entry.
"""
import asyncio
from decimal import Decimal
from datetime import datetime, timezone

from wins.shared.config import TRADE_MODE, EXCHANGE_BACKEND
from wins.shared.models import DecisionOutput, Action
from wins.shared.logger import get_logger
from wins.execution.exchange.base import ExchangeClient, OrderResult

log = get_logger("execution")


# ─── Paper executor ───────────────────────────────────────────────────────────

class PaperExecutor:
    """
    Simulates order execution for paper trading.
    SL/TP checking is handled by paper_portfolio.py on each cycle —
    no exchange API needed.
    """

    async def buy(
        self,
        decision: DecisionOutput,
        position_size_usd: Decimal,
    ) -> dict:
        qty = position_size_usd / decision.entry_price
        log.info(
            f"[PAPER BUY] {decision.token} qty={qty:.6f} "
            f"@ ${decision.entry_price} | SL=${decision.stop_loss_price} "
            f"TP=${decision.target_price} | size=${position_size_usd:.2f}"
        )
        return {
            "order_id":   f"paper_{decision.token}_{int(datetime.now(timezone.utc).timestamp())}",
            "token":      decision.token,
            "side":       "buy",
            "qty":        float(qty),
            "fill_price": float(decision.entry_price),
            "mode":       "paper",
        }

    async def sell(
        self,
        token: str,
        qty: Decimal,
        current_price: Decimal,
        reason: str,
    ) -> dict:
        log.info(f"[PAPER SELL] {token} qty={qty:.6f} @ ${current_price} reason={reason}")
        return {
            "order_id":   f"paper_{token}_{int(datetime.now(timezone.utc).timestamp())}",
            "token":      token,
            "side":       "sell",
            "qty":        float(qty),
            "fill_price": float(current_price),
            "mode":       "paper",
            "reason":     reason,
        }


# ─── Live executor ────────────────────────────────────────────────────────────

class LiveExecutor:
    """
    Real order execution via ExchangeClient.
    Places exchange-native stop-loss immediately after every buy.
    """

    def __init__(self, client: ExchangeClient) -> None:
        self._client = client

    async def buy(
        self,
        decision: DecisionOutput,
        position_size_usd: Decimal,
    ) -> dict:
        # 1. Market buy
        result = await self._client.place_market_buy(decision.token, position_size_usd)
        log.info(
            f"[LIVE BUY] {decision.token} qty={result.qty} @ ${result.fill_price} "
            f"order_id={result.order_id}"
        )

        # 2. Immediately place exchange-native stop-loss
        sl_result = await self._client.place_stop_loss(
            decision.token,
            result.qty,
            decision.stop_loss_price,
        )
        log.info(
            f"[LIVE SL PLACED] {decision.token} stop=${decision.stop_loss_price} "
            f"sl_order_id={sl_result.order_id}"
        )

        return {
            "order_id":      result.order_id,
            "sl_order_id":   sl_result.order_id,
            "token":         decision.token,
            "side":          "buy",
            "qty":           float(result.qty),
            "fill_price":    float(result.fill_price),
            "mode":          "live",
        }

    async def sell(
        self,
        token: str,
        qty: Decimal,
        current_price: Decimal,
        reason: str,
        sl_order_id: str | None = None,
    ) -> dict:
        # Cancel open stop-loss before market sell to avoid double-sell
        if sl_order_id:
            cancelled = await self._client.cancel_order(sl_order_id, token)
            if not cancelled:
                log.warning(f"Could not cancel SL order {sl_order_id} for {token} — proceeding with market sell.")

        result = await self._client.place_market_sell(token, qty)
        log.info(f"[LIVE SELL] {token} qty={result.qty} @ ${result.fill_price} reason={reason}")
        return {
            "order_id":   result.order_id,
            "token":      token,
            "side":       "sell",
            "qty":        float(result.qty),
            "fill_price": float(result.fill_price),
            "mode":       "live",
            "reason":     reason,
        }


# ─── Factory ─────────────────────────────────────────────────────────────────

def get_executor() -> PaperExecutor | LiveExecutor:
    if TRADE_MODE == "paper":
        return PaperExecutor()
    if TRADE_MODE == "live":
        return _build_live_executor()
    raise ValueError(f"Unknown TRADE_MODE: {TRADE_MODE}")


def _build_live_executor() -> LiveExecutor:
    if EXCHANGE_BACKEND == "binance":
        from wins.execution.exchange.binance_api import BinanceClient
        return LiveExecutor(BinanceClient())
    if EXCHANGE_BACKEND == "coinbase":
        from wins.execution.exchange.coinbase_api import CoinbaseClient
        return LiveExecutor(CoinbaseClient())
    raise ValueError(f"Unknown EXCHANGE_BACKEND: {EXCHANGE_BACKEND}. Options: binance | coinbase")
