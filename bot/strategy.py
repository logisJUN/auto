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
from bot.signals import aggregator, technical, universe
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

        self._pinned_symbols: list[str] = list(cfg.get("exchange", "symbols", default=["BTCUSDT"]))
        self.universe_cfg: dict = cfg.get("exchange", "universe", default={})
        self.symbols: list[str] = list(self._pinned_symbols)
        self._last_universe_scan = 0.0

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
        equity = self.client.get_equity_usdt()
        self.state.ensure_daily(equity)
        if self._daily_loss_breached():
            return

        signal = self.get_signal(symbol)
        if signal["atr"] <= 0 or signal["close"] <= 0:
            return

        min_conf = self.risk_cfg.get("min_confidence_to_enter", 0.55)
        is_trend_candidate = signal["direction"] != "neutral" and signal["confidence"] >= min_conf

        # "No room" means either every slot is used, or -- more commonly at a high
        # position_size_pct_of_equity + margin_buffer_pct combo -- there's simply no
        # margin headroom left even though a slot count is technically free.
        slots_full = self.state.open_trade_count() >= self.risk_cfg.get("max_concurrent_positions", 1)
        no_margin_room = self._margin_for_new_position(equity) <= 0
        if slots_full or no_margin_room:
            equity = self._make_room_for_override(symbol, signal, is_trend_candidate, equity)
            if equity is None:
                return  # no override -- stay on the sidelines this tick

        if is_trend_candidate:
            self._enter_trend(symbol, signal, equity)
        elif signal["direction"] == "neutral":
            self._enter_range(symbol, signal, equity)

    def _current_confidence_of_open(self, symbol: str, trade: dict) -> float:
        """Confidence of the position's own direction, from its *current* cached
        signal (not the confidence at entry time). A signal that has faded to
        neutral or reversed against the position scores lowest, since that
        position's original thesis no longer holds.
        """
        signal = self.get_signal(symbol)
        if signal["direction"] == trade["side"]:
            return signal["confidence"]
        if signal["direction"] == "neutral":
            return 0.0
        return -1.0  # signal has reversed against this position

    def _make_room_for_override(self, symbol: str, signal: dict, is_trend_candidate: bool,
                                 equity: float) -> float | None:
        """All slots are full. If `symbol`'s signal is a strong enough trend
        candidate, and it clearly beats the weakest currently-held position's
        *current* conviction, close that weakest position and free its slot/margin
        for this one. Returns refreshed equity to enter with, or None to skip.
        """
        override_cfg = self.risk_cfg.get("override_entry", {})
        if not override_cfg.get("enabled", False) or not is_trend_candidate:
            return None
        if signal["confidence"] < override_cfg.get("min_confidence", 0.85):
            return None

        trades = self.state.snapshot().get("trades", {})
        weakest_symbol, weakest_trade, weakest_conf = None, None, None
        for open_symbol, trade in trades.items():
            conf = self._current_confidence_of_open(open_symbol, trade)
            if weakest_conf is None or conf < weakest_conf:
                weakest_symbol, weakest_trade, weakest_conf = open_symbol, trade, conf
        if weakest_symbol is None:
            return None

        margin = override_cfg.get("min_confidence_margin_over_weakest", 0.15)
        if signal["confidence"] - weakest_conf < margin:
            return None

        logger.info("[교체진입] %s(conf=%.2f)가 기존 %s(conf=%.2f)보다 강해 교체합니다",
                     symbol, signal["confidence"], weakest_symbol, weakest_conf)
        self._close_and_settle(weakest_symbol, weakest_trade, "override_reallocation")
        return self.client.get_equity_usdt()  # refresh -- the close just realized PnL

    def _leverage_for(self, symbol: str, inst, confidence: float | None) -> float:
        lev_range = self.risk_cfg.get("leverage_by_symbol", {}).get(symbol)
        if lev_range is None:
            # not individually configured -- e.g. picked up by the dynamic
            # universe scan -- so use the catch-all range instead.
            lev_range = self.risk_cfg.get(
                "default_leverage_range", {"min": 1, "max": self.risk_cfg.get("max_leverage", 5)}
            )
        lev_min = lev_range.get("min", 1)
        lev_max = lev_range.get("max", self.risk_cfg.get("max_leverage", 5))
        max_leverage = min(lev_max, inst.max_leverage)
        lev_min = min(lev_min, max_leverage)
        if confidence is None:
            return max_leverage
        return max(lev_min, round(lev_min + confidence * (max_leverage - lev_min)))

    def _used_margin(self) -> float:
        """Margin already committed to currently open positions (qty*entry_price /
        leverage, summed), used to enforce the margin-buffer policy below.
        """
        total = 0.0
        for trade in self.state.snapshot().get("trades", {}).values():
            leverage = trade.get("leverage") or 1
            total += (trade["qty"] * trade["entry_price"]) / leverage
        return total

    def _margin_for_new_position(self, equity: float) -> float:
        """Target margin (position_size_pct_of_equity% of equity), capped so total
        margin in use never exceeds equity * (1 - margin_buffer_pct/100) -- i.e. a
        reserve is always kept free rather than every slot targeting its % of
        *total* equity independently and potentially over-committing.
        """
        buffer_pct = self.risk_cfg.get("margin_buffer_pct", 15.0)
        headroom = equity * (1 - buffer_pct / 100.0) - self._used_margin()
        target = equity * self.risk_cfg.get("position_size_pct_of_equity", 25.0) / 100.0
        return max(0.0, min(target, headroom))

    def _open(self, symbol: str, side: str, entry_price: float, margin: float,
              leverage: float, atr: float, trade_cfg: dict, extra_trade_fields: dict,
              log_extra: dict, msg_tag: str):
        if margin <= 0:
            logger.info("skip entry %s: no margin headroom left (buffer reserved)", symbol)
            return

        inst = self.client.get_instrument_info(symbol)
        prospective = stop_manager.new_trade(symbol, side, entry_price, 0.0, atr, trade_cfg)

        sizing = position_sizing.compute_qty_fixed_margin(
            margin=margin,
            leverage=leverage,
            entry_price=entry_price,
            qty_step=inst.qty_step,
            min_qty=inst.min_qty,
            min_notional=self.risk_cfg.get("min_order_notional_usdt", 5.0),
        )
        if not sizing.ok:
            logger.info("skip entry %s: %s", symbol, sizing.reason)
            return

        # compute_qty_fixed_margin's floor-based math can leave floating-point
        # noise (e.g. 4.1000000000000005) that Bybit rejects outright as an
        # invalid qty string -- round_qty formats it to the symbol's actual
        # step precision, same as round_price already does for SL/TP.
        qty = self.client.round_qty(symbol, sizing.qty)
        if qty < inst.min_qty:
            logger.info("skip entry %s: qty %.10g rounds below exchange minimum %.10g", symbol, qty, inst.min_qty)
            return

        try:
            self.client.set_leverage(symbol, leverage)
            sl_price = self.client.round_price(symbol, prospective["initial_sl"])
            tp_price = self.client.round_price(symbol, prospective["initial_tp"])
            self.client.open_position(symbol, side, qty, stop_loss=sl_price, take_profit=tp_price)
        except BybitAPIError as exc:
            logger.error("failed to open position for %s: %s", symbol, exc)
            log_decision(self.log_dir, {"event": "entry_failed", "symbol": symbol, "error": str(exc)})
            return

        # place_order's stopLoss/takeProfit params can silently fail to attach
        # even when the base market order fills, leaving a naked leveraged
        # position with no protective stop on the exchange. Verify and repair
        # before ever recording this as a tracked, "protected" trade.
        exchange_position = self.client.get_position(symbol)
        sl_tp_attached = bool(exchange_position) and exchange_position["stop_loss"] > 0 and exchange_position["take_profit"] > 0
        if not sl_tp_attached and not self._repair_sl_tp(symbol, sl_price, tp_price):
            logger.critical("%s opened with NO protective SL/TP and repair failed -- closing immediately", symbol)
            try:
                self.client.close_position(symbol, side, qty)
            except BybitAPIError as exc:
                logger.critical("COULD NOT CLOSE UNPROTECTED POSITION %s -- MANUAL ACTION REQUIRED: %s", symbol, exc)
            self.notifier.send(f"[긴급] {symbol} SL/TP 설정 실패 - 긴급 청산 시도했습니다. Bybit 앱에서 직접 확인하세요.")
            log_decision(self.log_dir, {"event": "sl_tp_attach_failed", "symbol": symbol})
            return

        trade = stop_manager.new_trade(symbol, side, entry_price, qty, atr, trade_cfg)
        trade["initial_sl"] = trade["current_sl"] = sl_price
        trade["initial_tp"] = trade["current_tp"] = tp_price
        trade["leverage"] = leverage
        trade.update(extra_trade_fields)
        self.state.set_trade(symbol, trade)

        msg = (f"[진입{msg_tag}] {symbol} {side.upper()} qty={qty} entry~{entry_price:.4f} "
               f"SL={sl_price:.4f} TP={tp_price:.4f} lev={leverage}x")
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "entry", "symbol": symbol, "side": side, "qty": sizing.qty,
                                     "entry_price": entry_price, "sl": sl_price, "tp": tp_price,
                                     "leverage": leverage, **log_extra})

    def _enter_trend(self, symbol: str, signal: dict, equity: float):
        side = signal["direction"]
        entry_price = self.client.get_last_price(symbol)
        inst = self.client.get_instrument_info(symbol)
        leverage = self._leverage_for(symbol, inst, signal["confidence"])
        margin = self._margin_for_new_position(equity)
        self._open(symbol, side, entry_price, margin, leverage, signal["atr"], self.trade_cfg,
                   extra_trade_fields={}, log_extra={"signal": signal},
                   msg_tag=f" conf={signal['confidence']:.2f}")

    def _enter_range(self, symbol: str, signal: dict, equity: float):
        range_cfg = self.trade_cfg.get("range_trade", {})
        if not range_cfg.get("enabled", False):
            return

        range_high = signal.get("range_high", 0.0)
        range_low = signal.get("range_low", 0.0)
        atr = signal["atr"]
        close = signal["close"]
        if range_high <= 0 or range_low <= 0 or range_high <= range_low:
            return

        # a neutral aggregate score doesn't guarantee price is actually ranging --
        # it can also happen mid-trend when sub-signals disagree. Skip if the
        # recent high-low band is too wide relative to ATR (that's expansion/
        # trend, not consolidation), so we don't fade a real breakout.
        max_width = atr * range_cfg.get("max_range_width_atr_mult", 4.0)
        if (range_high - range_low) > max_width:
            return

        edge = atr * range_cfg.get("edge_atr_mult", 0.5)
        if close <= range_low + edge:
            side = "long"
        elif close >= range_high - edge:
            side = "short"
        else:
            return  # price sits in the middle of the range -- no edge to fade

        entry_price = self.client.get_last_price(symbol)
        inst = self.client.get_instrument_info(symbol)
        leverage = self._leverage_for(symbol, inst, confidence=None)  # always use the symbol's max
        margin = self._margin_for_new_position(equity)
        self._open(symbol, side, entry_price, margin, leverage, atr, range_cfg,
                   extra_trade_fields={"is_range_trade": True},
                   log_extra={"range_high": range_high, "range_low": range_low},
                   msg_tag=":range")

    # -- exits / management ---------------------------------------------------------
    def _close_and_settle(self, symbol: str, trade: dict, reason: str, already_closed: bool = False):
        if not already_closed:
            try:
                close_qty = self.client.round_qty(symbol, trade["qty"])
                self.client.close_position(symbol, trade["side"], close_qty)
            except BybitAPIError as exc:
                logger.error("failed to close %s: %s", symbol, exc)
                log_decision(self.log_dir, {"event": "close_failed", "symbol": symbol, "error": str(exc)})
                return

        # Prefer the exchange's own closed-pnl record: it's net of trading fees and
        # uses the real average exit fill price, unlike estimating from last price.
        # It can lag a close by a few seconds, so fall back to a price-based
        # estimate (fees not included) if there's no record yet or the lookup fails.
        closed = self.client.get_closed_pnl(symbol)
        if closed and closed["updated_time_ms"] / 1000.0 >= trade["opened_at"]:
            pnl = closed["closed_pnl"]
            exit_price = closed["avg_exit_price"] or trade["entry_price"]
            pnl_is_estimate = False
        else:
            try:
                exit_price = self.client.get_last_price(symbol)
            except BybitAPIError:
                exit_price = trade["entry_price"]
            if trade["side"] == "long":
                pnl = (exit_price - trade["entry_price"]) * trade["qty"]
            else:
                pnl = (trade["entry_price"] - exit_price) * trade["qty"]
            pnl_is_estimate = True
            logger.warning("no exchange closed-pnl record yet for %s, using price-based "
                            "estimate (fees not included)", symbol)

        self.state.add_realized_pnl(pnl)
        self.state.set_trade(symbol, None)
        self.state.record_closed_trade({
            "symbol": symbol, "side": trade["side"], "entry_price": trade["entry_price"],
            "exit_price": exit_price, "qty": trade["qty"], "pnl": pnl, "reason": reason,
            "pnl_is_estimate": pnl_is_estimate, "closed_at": time.time(),
        })

        est_tag = " (est.)" if pnl_is_estimate else ""
        msg = f"[종료:{reason}] {symbol} {trade['side'].upper()} exit~{exit_price:.4f} PnL={pnl:+.4f} USDT{est_tag}"
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "exit", "symbol": symbol, "reason": reason,
                                     "exit_price": exit_price, "pnl": pnl, "pnl_is_estimate": pnl_is_estimate})

    def _repair_sl_tp(self, symbol: str, sl_price: float, tp_price: float) -> bool:
        """Attempts to (re)attach SL/TP to a position that's missing one or both
        on the exchange. Returns False only if the repair call itself fails --
        callers are expected to treat that as serious (an unprotected leveraged
        position) and close out rather than keep holding it.
        """
        logger.error("%s missing SL/TP on the exchange -- attempting to reattach", symbol)
        try:
            self.client.update_trading_stop(symbol, stop_loss=sl_price, take_profit=tp_price)
            return True
        except BybitAPIError as exc:
            logger.critical("failed to reattach SL/TP for %s: %s", symbol, exc)
            return False

    def manage_open_position(self, symbol: str):
        trade = self.state.get_trade(symbol)
        if trade is None:
            return

        exchange_position = self.client.get_position(symbol)
        if exchange_position is None:
            # SL or TP was hit on the exchange side since our last check -- there is
            # nothing left to close, so don't place a reduce-only order against a
            # position that's already zero (Bybit rejects it with ErrCode 110017).
            self._close_and_settle(symbol, trade, "sl_tp_hit", already_closed=True)
            return

        if exchange_position["stop_loss"] <= 0 or exchange_position["take_profit"] <= 0:
            # Ongoing safety net, not just at entry: repair or, failing that, exit
            # rather than keep holding an unprotected leveraged position.
            if not self._repair_sl_tp(symbol, trade["current_sl"], trade["current_tp"]):
                logger.critical("%s still has no SL/TP after repair attempt -- closing for safety", symbol)
                self.notifier.send(f"[긴급] {symbol} SL/TP 재설정 실패 - 안전을 위해 청산합니다. 직접 확인하세요.")
                self._close_and_settle(symbol, trade, "sl_tp_missing")
                return

        price = self.client.get_last_price(symbol)
        window = self.trade_cfg.get("flash_move_window_sec", 45)
        self.flash_tracker.record(symbol, price, window)

        entry_atr = trade.get("entry_atr", 0.0)
        # entry_atr is missing on trades opened before this field existed --
        # fall back to the old fixed 1.2% behavior for those until they close.
        entry_atr_pct = (entry_atr / trade["entry_price"] * 100.0) if entry_atr > 0 and trade["entry_price"] > 0 else 1.2
        if stop_manager.check_flash_move(self.flash_tracker, symbol, trade["side"], self.trade_cfg, entry_atr_pct):
            self._close_and_settle(symbol, trade, "emergency_flash_move")
            return

        signal = self.get_signal(symbol)  # uses cache; refreshed on its own cadence

        if stop_manager.check_signal_reversal(trade, signal, self.trade_cfg):
            self._close_and_settle(symbol, trade, "signal_reversal")
            return

        if stop_manager.check_stale_position(trade, price, self.trade_cfg):
            self._close_and_settle(symbol, trade, "stale_timeout")
            return

        if trade.get("is_range_trade"):
            # range/scalp trades exit only via their (tight) exchange SL/TP, the
            # shared flash-move/reversal/stale-timeout checks above -- no riding
            # the trade further with breakeven/trailing/TP-extension.
            self.state.set_trade(symbol, trade)
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
    def _refresh_universe(self):
        """Re-screens the tradable universe by 24h turnover on its own cadence
        (universe.rescan_interval_hours). A symbol whose position is currently
        open is managed until it closes regardless of whether it's still in the
        refreshed watchlist -- this only changes what's eligible for new entries.
        """
        if not self.universe_cfg.get("enabled", False):
            return
        now = time.time()
        interval_sec = self.universe_cfg.get("rescan_interval_hours", 1) * 3600
        if self._last_universe_scan and now - self._last_universe_scan < interval_sec:
            return

        try:
            top_n = self.universe_cfg.get("top_n", 30)
            screened = universe.screen_top_symbols(self.client, quote_suffix="USDT", top_n=top_n)
        except Exception:
            logger.exception("universe screening failed -- keeping current watchlist")
            self._last_universe_scan = now  # don't retry every single tick on a persistent error
            return

        merged = list(dict.fromkeys(self._pinned_symbols + screened))
        added = sorted(set(merged) - set(self.symbols))
        dropped = sorted(set(self.symbols) - set(merged))
        self.symbols = merged
        self._last_universe_scan = now
        logger.info("universe refreshed: %d symbols (added=%s, dropped=%s)", len(merged), added, dropped)

    def tick(self):
        self._refresh_universe()

        open_symbols = set(self.state.snapshot().get("trades", {}).keys())
        symbols_to_check = list(dict.fromkeys(self.symbols + list(open_symbols)))

        for symbol in symbols_to_check:
            try:
                if self.state.get_trade(symbol) is not None:
                    self.manage_open_position(symbol)
                else:
                    self.try_enter(symbol)
            except BybitAPIError as exc:
                logger.error("bybit API error on %s: %s", symbol, exc)
            except Exception:
                logger.exception("unexpected error handling %s", symbol)
