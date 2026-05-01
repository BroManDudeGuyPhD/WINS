"""
wins/execution/exchange/coinbase_api.py
Coinbase Advanced Trade client stub.

API key permissions required (Coinbase dashboard):
  - View          ✓
  - Trade         ✓
  - Transfer      ✗ (never needed)
  - IP allowlist  ✓ (strongly recommended)

Stop-loss strategy on Coinbase:
  Coinbase Advanced Trade supports STOP_LIMIT orders (similar to Binance).
  Place immediately after every buy via place_stop_loss().

Implementation status: STUB — wire up coinbase-advanced-py SDK when ready.
Install: pip install coinbase-advanced-py
"""
from __future__ import annotations
from decimal import Decimal

from wins.execution.exchange.base import ExchangeClient, OrderResult, AccountBalance
from wins.shared.logger import get_logger

log = get_logger("exchange.coinbase")


class CoinbaseClient(ExchangeClient):
    """Coinbase Advanced Trade — not yet implemented."""

    def __init__(self) -> None:
        log.warning("CoinbaseClient is a stub. Switch EXCHANGE_BACKEND=binance for live trading.")

    async def get_balance(self) -> AccountBalance:
        raise NotImplementedError("CoinbaseClient not implemented. Use EXCHANGE_BACKEND=binance.")

    async def get_ticker_price(self, token: str) -> Decimal:
        raise NotImplementedError

    async def place_market_buy(self, token: str, quote_amount: Decimal) -> OrderResult:
        raise NotImplementedError

    async def place_market_sell(self, token: str, qty: Decimal) -> OrderResult:
        raise NotImplementedError

    async def place_stop_loss(self, token: str, qty: Decimal, stop_price: Decimal) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, order_id: str, token: str) -> bool:
        raise NotImplementedError
