"""Thin, defensive wrapper around Bybit's V5 unified-trading REST API (via pybit).

Every method raises `BybitAPIError` on a logical failure (ret_code != 0) and retries
a few times on transient network errors before giving up. Nothing here decides
*when* to trade -- that's strategy.py. This module only knows how to talk to Bybit.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP
from requests.exceptions import RequestException

logger = logging.getLogger("bot.exchange")


class BybitAPIError(RuntimeError):
    pass


@dataclass
class InstrumentInfo:
    symbol: str
    qty_step: float
    min_qty: float
    tick_size: float
    max_leverage: float


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool, category: str = "linear"):
        self.category = category
        self.session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        self._instrument_cache: dict[str, InstrumentInfo] = {}

    # -- low level ---------------------------------------------------------
    def _call(self, fn, retryable: bool = True, **kwargs):
        """Calls a pybit method. pybit itself raises InvalidRequestError/FailedRequestError
        for logical API errors (bad params, rejected order, etc.) -- those are never retried,
        since retrying the same bad request just fails again. Only genuine network errors are
        retried, and only when `retryable=True`: non-idempotent calls like placing/closing an
        order pass retryable=False so a timeout can never cause a duplicate market order.
        """
        name = getattr(fn, "__name__", str(fn))
        attempts = 3 if retryable else 1
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                resp = fn(**kwargs)
                return resp.get("result", {})
            except (InvalidRequestError, FailedRequestError) as exc:
                raise BybitAPIError(f"{name} rejected by Bybit: {exc}") from exc
            except RequestException as exc:
                last_exc = exc
                if i == attempts - 1:
                    break
                wait = 1.5 * (2 ** i)
                logger.warning("network error calling %s (attempt %d/%d): %s. retrying in %.1fs",
                                name, i + 1, attempts, exc, wait)
                time.sleep(wait)
        raise BybitAPIError(f"{name} network error after {attempts} attempt(s): {last_exc}")

    # -- market data ---------------------------------------------------------
    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[dict]:
        """Returns oldest->newest list of {ts, open, high, low, close, volume}."""
        result = self._call(
            self.session.get_kline,
            category=self.category, symbol=symbol, interval=interval, limit=limit,
        )
        rows = result.get("list", [])
        candles = [
            {
                "ts": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in rows
        ]
        candles.reverse()  # bybit returns newest-first
        return candles

    def get_last_price(self, symbol: str) -> float:
        result = self._call(self.session.get_tickers, category=self.category, symbol=symbol)
        lst = result.get("list", [])
        if not lst:
            raise BybitAPIError(f"no ticker data for {symbol}")
        return float(lst[0]["lastPrice"])

    def get_instrument_info(self, symbol: str) -> InstrumentInfo:
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        result = self._call(self.session.get_instruments_info, category=self.category, symbol=symbol)
        lst = result.get("list", [])
        if not lst:
            raise BybitAPIError(f"no instrument info for {symbol}")
        info = lst[0]
        lot = info["lotSizeFilter"]
        price = info["priceFilter"]
        leverage = info.get("leverageFilter", {})
        inst = InstrumentInfo(
            symbol=symbol,
            qty_step=float(lot["qtyStep"]),
            min_qty=float(lot["minOrderQty"]),
            tick_size=float(price["tickSize"]),
            max_leverage=float(leverage.get("maxLeverage", 1)),
        )
        self._instrument_cache[symbol] = inst
        return inst

    # -- account ---------------------------------------------------------
    def get_equity_usdt(self) -> float:
        result = self._call(self.session.get_wallet_balance, accountType="UNIFIED")
        lst = result.get("list", [])
        if not lst:
            raise BybitAPIError("no wallet balance data")
        return float(lst[0]["totalEquity"])

    def get_position(self, symbol: str) -> dict | None:
        result = self._call(self.session.get_positions, category=self.category, symbol=symbol)
        for p in result.get("list", []):
            if float(p.get("size") or 0) > 0:
                return {
                    "symbol": p["symbol"],
                    "side": p["side"],  # "Buy" (long) or "Sell" (short)
                    "size": float(p["size"]),
                    "entry_price": float(p["avgPrice"]),
                    "unrealized_pnl": float(p.get("unrealisedPnl") or 0),
                    "position_idx": int(p.get("positionIdx") or 0),
                }
        return None

    def get_closed_pnl(self, symbol: str) -> dict | None:
        """Returns the exchange's own record for the most recently closed position
        on `symbol`, whose closedPnl is net of trading fees (unlike computing
        entry/exit price difference ourselves, which ignores fees). Returns None
        if no record is found yet (can lag a close by a few seconds) or on error --
        callers should fall back to an estimate in that case.
        """
        try:
            result = self._call(self.session.get_closed_pnl, category=self.category, symbol=symbol, limit=1)
        except BybitAPIError:
            return None
        lst = result.get("list", [])
        if not lst:
            return None
        rec = lst[0]
        try:
            return {
                "closed_pnl": float(rec["closedPnl"]),
                "avg_exit_price": float(rec["avgExitPrice"]),
                "updated_time_ms": int(rec["updatedTime"]),
            }
        except (KeyError, ValueError, TypeError):
            return None

    def set_leverage(self, symbol: str, leverage: float) -> None:
        lev = str(int(leverage))
        try:
            self._call(
                self.session.set_leverage,
                category=self.category, symbol=symbol, buyLeverage=lev, sellLeverage=lev,
            )
        except BybitAPIError as exc:
            if "leverage not modified" in str(exc).lower():
                return
            raise

    # -- trading ---------------------------------------------------------
    def round_qty(self, symbol: str, qty: float) -> float:
        inst = self.get_instrument_info(symbol)
        step = inst.qty_step
        rounded = math.floor(qty / step) * step
        decimals = max(0, len(str(step).split(".")[-1])) if "." in str(step) else 0
        return round(max(rounded, 0.0), decimals)

    def round_price(self, symbol: str, price: float) -> float:
        inst = self.get_instrument_info(symbol)
        tick = inst.tick_size
        rounded = round(price / tick) * tick
        decimals = max(0, len(str(tick).split(".")[-1])) if "." in str(tick) else 0
        return round(rounded, decimals)

    def open_position(self, symbol: str, side: str, qty: float,
                       stop_loss: float | None = None, take_profit: float | None = None) -> dict:
        """side: 'long' or 'short'."""
        order_side = "Buy" if side == "long" else "Sell"
        kwargs = dict(
            category=self.category, symbol=symbol, side=order_side,
            orderType="Market", qty=str(qty), timeInForce="IOC", positionIdx=0,
        )
        if stop_loss is not None:
            kwargs["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            kwargs["takeProfit"] = str(take_profit)
        return self._call(self.session.place_order, retryable=False, **kwargs)

    def close_position(self, symbol: str, side: str, qty: float) -> dict:
        """Market-closes an open position. side is the side of the OPEN position."""
        order_side = "Sell" if side == "long" else "Buy"
        return self._call(
            self.session.place_order, retryable=False,
            category=self.category, symbol=symbol, side=order_side,
            orderType="Market", qty=str(qty), reduceOnly=True,
            timeInForce="IOC", positionIdx=0,
        )

    def update_trading_stop(self, symbol: str, stop_loss: float | None = None,
                             take_profit: float | None = None) -> dict:
        kwargs = dict(category=self.category, symbol=symbol, positionIdx=0, tpslMode="Full")
        if stop_loss is not None:
            kwargs["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            kwargs["takeProfit"] = str(take_profit)
        return self._call(self.session.set_trading_stop, **kwargs)
