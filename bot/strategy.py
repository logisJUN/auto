"""Orchestrates everything: turns cached signals into entries, and manages open
positions tick by tick (flash-move emergency exit, trailing SL, TP extension,
signal-reversal exit). This is the only module that decides to place or close
an order; exchange/bybit_client.py only knows how to execute what it's told.
"""
from __future__ import annotations

import logging
import time

from bot.exchange.bybit_client import BybitClient, BybitAPIError
from bot.logger import log_decision
from bot.notify import Notifier
from bot.risk import position_sizing, stop_manager
from bot.signals import aggregator, technical
from bot.signals.news import NewsSignal
from bot.signals.polymarket import PolymarketSignal
from bot.state import StateStore

logger = logging.getLogger("bot.strategy")


class SignalCache:
    def __init__(self):
        self.last_refresh = 0.0
        self.result: dict | None = None


class Strategy:
    def __init__(self, client: BybitClient, cfg, state: StateStore, notifier: Notifier, log_dir: str):
        self.client = client
        self.cfg = cfg
        self.state = state
        self.notifier = notifier
        self.log_dir = log_dir

        self.symbols: list[str] = cfg.get("exchange", "symbols", default=["BTCUSDT"])
        self.risk_cfg: dict = cfg.get("risk", default={})
        self.signals_cfg: dict = cfg.get("signals", default={})
        self.trade_cfg: dict = cfg.get("trade_management", default={})

        self.news_signal = NewsSignal(self.signals_cfg.get("news", {}), cfg.secrets.newsapi_key)
        self.polymarket_signal = PolymarketSignal(self.signals_cfg.get("polymarket", {}))
        self.flash_tracker = stop_manager.FlashMoveTracker()
        self._signal_cache: dict[str, SignalCache] = {s: SignalCache() for s in self.symbols}

    # -- signal computation ---------------------------------------------------------
    def _fetch_klines_multi(self, symbol: str) -> dict[str, list[dict]]:
        tech_cfg = self.signals_cfg.get("technical", {})
        timeframes = tech_cfg.get("timeframes", ["15", "60", "240"])
        limit = tech_cfg.get("kline_limit", 200)
        out = {}
        for tf in timeframes:
            out[tf] = self.client.get_klines(symbol, tf, limit)
        return out

    def _compute_signal(self, symbol: str) -> dict:
        tech_cfg = self.signals_cfg.get("technical", {})
        timeframes = tech_cfg.get("timeframes", ["15", "60", "240"])
        klines = self._fetch_klines_multi(symbol)
        tech = technical.multi_timeframe_score(klines, timeframes, tech_cfg)
        news = self.news_signal.score_for_symbol(symbol)
        poly = self.polymarket_signal.score()
        weights = self.signals_cfg.get("weights", {})
        return aggregator.aggregate(tech, news, poly, weights)

    def get_signal(self, symbol: str, force: bool = False) -> dict:
        cache = self._signal_cache.setdefault(symbol, SignalCache())
        refresh_sec = self.cfg.get("loop", "signal_refresh_sec", default=120)
        now = time.time()
        if force or cache.result is None or (now - cache.last_refresh) >= refresh_sec:
            try:
                cache.result = self._compute_signal(symbol)
                cache.last_refresh = now
            except Exception as exc:
                logger.exception("signal computation failed for %s: %s", symbol, exc)
                if cache.result is None:
                    cache.result = {"score": 0.0, "confidence": 0.0, "direction": "neutral",
                                     "atr": 0.0, "close": 0.0, "components": {}}
        return cache.result

    # -- entries ---------------------------------------------------------
    def _daily_loss_breached(self) -> bool:
        limit = self.risk_cfg.get("max_daily_loss_pct", 8.0)
        return self.state.daily_loss_pct() >= limit

    def try_enter(self, symbol: str):
        if self.state.get_trade(symbol) is not None:
            return
        if self.state.open_trade_count() >= self.risk_cfg.get("max_concurrent_positions", 1):
            return
        equity = self.client.get_equity_usdt()
        self.state.ensure_daily(equity)
        if self._daily_loss_breached():
            return

        signal = self.get_signal(symbol)
        min_conf = self.risk_cfg.get("min_confidence_to_enter", 0.55)
        if signal["direction"] == "neutral" or signal["confidence"] < min_conf:
            return
        if signal["atr"] <= 0 or signal["close"] <= 0:
            return

        side = signal["direction"]
        entry_price = self.client.get_last_price(symbol)
        prospective = stop_manager.new_trade(symbol, side, entry_price, 0.0, signal["atr"], self.trade_cfg)

        inst = self.client.get_instrument_info(symbol)
        max_leverage = min(self.risk_cfg.get("max_leverage", 5), inst.max_leverage)
        leverage = max(1, round(1 + signal["confidence"] * (max_leverage - 1)))

        sizing = position_sizing.compute_qty(
            equity=equity,
            risk_per_trade_pct=self.risk_cfg.get("risk_per_trade_pct", 1.5),
            entry_price=entry_price,
            stop_loss_price=prospective["initial_sl"],
            max_leverage=leverage,
            qty_step=inst.qty_step,
            min_qty=inst.min_qty,
            min_notional=self.risk_cfg.get("min_order_notional_usdt", 5.0),
        )
        if not sizing.ok:
            logger.info("skip entry %s: %s", symbol, sizing.reason)
            return

        try:
            self.client.set_leverage(symbol, leverage)
            sl_price = self.client.round_price(symbol, prospective["initial_sl"])
            tp_price = self.client.round_price(symbol, prospective["initial_tp"])
            self.client.open_position(symbol, side, sizing.qty, stop_loss=sl_price, take_profit=tp_price)
        except BybitAPIError as exc:
            logger.error("failed to open position for %s: %s", symbol, exc)
            log_decision(self.log_dir, {"event": "entry_failed", "symbol": symbol, "error": str(exc)})
            return

        trade = stop_manager.new_trade(symbol, side, entry_price, sizing.qty, signal["atr"], self.trade_cfg)
        trade["initial_sl"] = trade["current_sl"] = sl_price
        trade["initial_tp"] = trade["current_tp"] = tp_price
        trade["leverage"] = leverage
        self.state.set_trade(symbol, trade)

        msg = (f"[진입] {symbol} {side.upper()} qty={sizing.qty} entry~{entry_price:.4f} "
               f"SL={sl_price:.4f} TP={tp_price:.4f} lev={leverage}x conf={signal['confidence']:.2f}")
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "entry", "symbol": symbol, "side": side, "qty": sizing.qty,
                                     "entry_price": entry_price, "sl": sl_price, "tp": tp_price,
                                     "leverage": leverage, "signal": signal})

    # -- exits / management ---------------------------------------------------------
    def _close_and_settle(self, symbol: str, trade: dict, reason: str):
        try:
            self.client.close_position(symbol, trade["side"], trade["qty"])
        except BybitAPIError as exc:
            logger.error("failed to close %s: %s", symbol, exc)
            log_decision(self.log_dir, {"event": "close_failed", "symbol": symbol, "error": str(exc)})
            return

        try:
            exit_price = self.client.get_last_price(symbol)
        except BybitAPIError:
            exit_price = trade["entry_price"]

        if trade["side"] == "long":
            pnl = (exit_price - trade["entry_price"]) * trade["qty"]
        else:
            pnl = (trade["entry_price"] - exit_price) * trade["qty"]

        self.state.add_realized_pnl(pnl)
        self.state.set_trade(symbol, None)
        self.state.record_closed_trade({
            "symbol": symbol, "side": trade["side"], "entry_price": trade["entry_price"],
            "exit_price": exit_price, "qty": trade["qty"], "pnl": pnl, "reason": reason,
            "closed_at": time.time(),
        })

        msg = f"[종료:{reason}] {symbol} {trade['side'].upper()} exit~{exit_price:.4f} PnL={pnl:+.4f} USDT"
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "exit", "symbol": symbol, "reason": reason,
                                     "exit_price": exit_price, "pnl": pnl})

    def manage_open_position(self, symbol: str):
        trade = self.state.get_trade(symbol)
        if trade is None:
            return

        exchange_position = self.client.get_position(symbol)
        if exchange_position is None:
            # SL or TP was hit on the exchange side since our last check.
            self._close_and_settle(symbol, trade, "sl_tp_hit")
            return

        price = self.client.get_last_price(symbol)
        window = self.trade_cfg.get("flash_move_window_sec", 45)
        self.flash_tracker.record(symbol, price, window)

        if stop_manager.check_flash_move(self.flash_tracker, symbol, trade["side"], self.trade_cfg):
            self._close_and_settle(symbol, trade, "emergency_flash_move")
            return

        signal = self.get_signal(symbol)  # uses cache; refreshed on its own cadence

        if stop_manager.check_signal_reversal(trade, signal, self.trade_cfg):
            self._close_and_settle(symbol, trade, "signal_reversal")
            return

        atr = signal.get("atr") or trade["risk_distance"]
        update = stop_manager.update_trailing_and_tp(trade, price, atr, signal, self.trade_cfg)
        if update["sl_changed"] or update["tp_changed"]:
            try:
                sl_price = self.client.round_price(symbol, trade["current_sl"])
                tp_price = self.client.round_price(symbol, trade["current_tp"])
                self.client.update_trading_stop(symbol, stop_loss=sl_price, take_profit=tp_price)
                trade["current_sl"], trade["current_tp"] = sl_price, tp_price
                self.state.set_trade(symbol, trade)
                logger.info("[조정] %s SL=%.4f TP=%.4f (%s)", symbol, sl_price, tp_price, update["reason"])
                log_decision(self.log_dir, {"event": "adjust", "symbol": symbol, "sl": sl_price,
                                             "tp": tp_price, "reason": update["reason"]})
            except BybitAPIError as exc:
                logger.error("failed to update trading stop for %s: %s", symbol, exc)
        else:
            self.state.set_trade(symbol, trade)

    # -- one full tick over all symbols ---------------------------------------------------------
    def tick(self):
        for symbol in self.symbols:
            try:
                if self.state.get_trade(symbol) is not None:
                    self.manage_open_position(symbol)
                else:
                    self.try_enter(symbol)
            except BybitAPIError as exc:
                logger.error("bybit API error on %s: %s", symbol, exc)
            except Exception:
                logger.exception("unexpected error handling %s", symbol)
