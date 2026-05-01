"""
wins/execution/exchange/base.py
Abstract exchange client interface.
Both paper and live executors implement this contract.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class OrderResult:
    order_id:    str
    token:       str
    side:        str          # buy | sell
    qty:         Decimal
    fill_price:  Decimal
    status:      str          # filled | pending | cancelled
    raw:         dict         # raw exchange response for logging


@dataclass
class AccountBalance:
    total_usd:     Decimal
    available_usd: Decimal
    positions:     dict[str, Decimal]   # token → qty held


class ExchangeClient(ABC):
    """
    Minimal exchange interface required by WINS execution layer.
    Each method documents the exchange-native stop-loss approach.
    """

    @abstractmethod
    async def get_balance(self) -> AccountBalance:
        """Return current account balance and open positions."""
        ...

    @abstractmethod
    async def place_market_buy(
        self,
        token:          str,
        quote_amount:   Decimal,        # USD amount to spend
    ) -> OrderResult:
        """Place a market buy order for `quote_amount` USD of `token`."""
        ...

    @abstractmethod
    async def place_market_sell(
        self,
        token:  str,
        qty:    Decimal,                # token quantity to sell
    ) -> OrderResult:
        """Place a market sell order for `qty` units of `token`."""
        ...

    @abstractmethod
    async def place_stop_loss(
        self,
        token:       str,
        qty:         Decimal,
        stop_price:  Decimal,
    ) -> OrderResult:
        """
        Place an exchange-native stop-market sell order.
        This is submitted immediately after every buy and is the primary
        loss protection mechanism — it fires even if the server goes offline.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, token: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        ...

    @abstractmethod
    async def get_ticker_price(self, token: str) -> Decimal:
        """Return the current mid-market price for `token` in USD."""
        ...
