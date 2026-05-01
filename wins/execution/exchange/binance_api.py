"""
wins/execution/exchange/binance_api.py
Binance exchange client.

Uses the official `binance-connector-python` SDK.
All spot trading — no futures, no leverage.

API key permissions required (Binance dashboard):
  - Read info          ✓ (for balance, price checks)
  - Spot trading       ✓ (for orders)
  - Withdraw           ✗ (never needed — do not enable)
  - IP whitelist       ✓ (strongly recommended — whitelist your server IP)

Stop-loss strategy:
  After every buy, a STOP_LOSS_LIMIT order is placed immediately.
  This fires at the exchange level even if the WINS server goes offline.
  On position close (target hit), the open stop order is cancelled first.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_DOWN
import asyncio

from wins.execution.exchange.base import ExchangeClient, OrderResult, AccountBalance
from wins.shared.config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET,
)
from wins.shared.logger import get_logger

log = get_logger("exchange.binance")

# Symbol format: "SOLUSDT", "BTCUSDT" etc.
def _pair(token: str) -> str:
    return f"{token.upper()}USDT"


class BinanceClient(ExchangeClient):
    """
    Thin async wrapper around the synchronous binance-connector SDK.
    Runs blocking SDK calls in a thread pool to stay non-blocking.

    Pass testnet=True (or set BINANCE_TESTNET=true in env) to route all
    orders to https://testnet.binance.vision using separate testnet credentials.
    """

    LIVE_URL    = "https://api.binance.com"
    TESTNET_URL = "https://testnet.binance.vision"

    def __init__(self, testnet: bool = False) -> None:
        # Import here so the module loads even without the package installed
        try:
            from binance.spot import Spot
        except ImportError:
            raise ImportError(
                "binance-connector not installed. "
                "Add 'binance-connector' to requirements.txt and rebuild."
            )
        self._testnet = testnet
        if testnet:
            key    = BINANCE_TESTNET_API_KEY
            secret = BINANCE_TESTNET_API_SECRET
            url    = self.TESTNET_URL
            log.info("[BINANCE] Using TESTNET — no real funds involved.")
        else:
            key    = BINANCE_API_KEY
            secret = BINANCE_API_SECRET
            url    = self.LIVE_URL

        self._client = Spot(
            api_key=key,
            api_secret=secret,
            base_url=url,
        )

    async def _run(self, fn, *args, **kwargs):
        """Run a synchronous SDK call in the default thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def get_balance(self) -> AccountBalance:
        account = await self._run(self._client.account)
        balances = account.get("balances", [])
        positions: dict[str, Decimal] = {}
        usdt_free = Decimal("0")
        total_usd = Decimal("0")

        for b in balances:
            asset = b["asset"]
            free  = Decimal(b["free"])
            if asset == "USDT":
                usdt_free = free
                total_usd += free
            elif free > 0:
                positions[asset] = free

        return AccountBalance(
            total_usd     = total_usd,
            available_usd = usdt_free,
            positions     = positions,
        )

    async def get_ticker_price(self, token: str) -> Decimal:
        resp = await self._run(self._client.ticker_price, _pair(token))
        return Decimal(str(resp["price"]))

    async def _get_lot_size(self, token: str) -> tuple[Decimal, Decimal]:
        """Return (min_qty, step_size) from exchange info for the symbol."""
        info = await self._run(self._client.exchange_info, symbol=_pair(token))
        for filt in info["symbols"][0]["filters"]:
            if filt["filterType"] == "LOT_SIZE":
                return Decimal(filt["minQty"]), Decimal(filt["stepSize"])
        return Decimal("0.001"), Decimal("0.001")

    async def _get_tick_size(self, token: str) -> Decimal:
        """Return the PRICE_FILTER tickSize for the symbol (controls price precision)."""
        info = await self._run(self._client.exchange_info, symbol=_pair(token))
        for filt in info["symbols"][0]["filters"]:
            if filt["filterType"] == "PRICE_FILTER":
                return Decimal(filt["tickSize"])
        return Decimal("0.01")

    def _round_qty(self, qty: Decimal, step: Decimal) -> Decimal:
        """Round qty down to the nearest step size."""
        return (qty // step) * step

    async def place_market_buy(self, token: str, quote_amount: Decimal) -> OrderResult:
        symbol = _pair(token)
        params = {
            "symbol":          symbol,
            "side":            "BUY",
            "type":            "MARKET",
            "quoteOrderQty":   str(quote_amount.quantize(Decimal("0.01"))),
        }
        log.info(f"[BINANCE] Market BUY {symbol} quoteQty={quote_amount}")
        raw = await self._run(self._client.new_order, **params)
        fills     = raw.get("fills", [{}])
        avg_price = Decimal(str(fills[0].get("price", 0))) if fills else Decimal("0")
        qty       = Decimal(str(raw.get("executedQty", "0")))
        return OrderResult(
            order_id   = str(raw["orderId"]),
            token      = token,
            side       = "buy",
            qty        = qty,
            fill_price = avg_price,
            status     = raw.get("status", "filled").lower(),
            raw        = raw,
        )

    async def place_market_sell(self, token: str, qty: Decimal) -> OrderResult:
        symbol = _pair(token)
        _, step = await self._get_lot_size(token)
        qty_rounded = self._round_qty(qty, step)
        params = {
            "symbol":   symbol,
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": str(qty_rounded),
        }
        log.info(f"[BINANCE] Market SELL {symbol} qty={qty_rounded}")
        raw = await self._run(self._client.new_order, **params)
        fills     = raw.get("fills", [{}])
        avg_price = Decimal(str(fills[0].get("price", 0))) if fills else Decimal("0")
        return OrderResult(
            order_id   = str(raw["orderId"]),
            token      = token,
            side       = "sell",
            qty        = Decimal(str(raw.get("executedQty", "0"))),
            fill_price = avg_price,
            status     = raw.get("status", "filled").lower(),
            raw        = raw,
        )

    async def place_stop_loss(
        self,
        token:      str,
        qty:        Decimal,
        stop_price: Decimal,
    ) -> OrderResult:
        """
        Places a STOP_LOSS_LIMIT order.
        limit_price = stop_price × 0.995 (0.5% below stop — ensures fill in fast drops).
        Both prices are rounded to the exchange's PRICE_FILTER tickSize.
        """
        symbol      = _pair(token)
        _, step     = await self._get_lot_size(token)
        tick        = await self._get_tick_size(token)
        qty_rounded = self._round_qty(qty, step)
        stop_rounded  = (stop_price  // tick) * tick
        limit_rounded = (stop_rounded * Decimal("0.995") // tick) * tick

        params = {
            "symbol":        symbol,
            "side":          "SELL",
            "type":          "STOP_LOSS_LIMIT",
            "quantity":      str(qty_rounded),
            "stopPrice":     str(stop_rounded),
            "price":         str(limit_rounded),
            "timeInForce":   "GTC",
        }
        log.info(f"[BINANCE] Stop-loss SELL {symbol} qty={qty_rounded} stop=${stop_rounded}")
        raw = await self._run(self._client.new_order, **params)
        return OrderResult(
            order_id   = str(raw["orderId"]),
            token      = token,
            side       = "sell",
            qty        = qty_rounded,
            fill_price = stop_rounded,   # not yet filled
            status     = raw.get("status", "new").lower(),
            raw        = raw,
        )

    async def cancel_order(self, order_id: str, token: str) -> bool:
        try:
            await self._run(self._client.cancel_order, symbol=_pair(token), orderId=int(order_id))
            return True
        except Exception as exc:
            log.warning(f"[BINANCE] Cancel order {order_id} failed: {exc}")
            return False
