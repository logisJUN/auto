"""Orchestrates everything: turns cached signals into entries, and manages open
positions tick by tick (flash-move emergency exit, trailing SL, TP extension,
signal-reversal exit). This is the only module that decides to place or close
an order; exchange/bybit_client.py only knows how to execute what it's told.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from bot.email_notify import EmailNotifier
from bot.exchange.bybit_client import BybitClient, BybitAPIError
from bot.logger import compute_performance_summary, log_decision
from bot.notify import Notifier
from bot.risk import position_sizing, stop_manager
from bot.signals import aggregator, funding, technical, universe
from bot.signals.news import NewsSignal
from bot.signals.polymarket import PolymarketSignal
from bot.state import StateStore

logger = logging.getLogger("bot.strategy")

# Bybit error codes that mean "this will never work, not just right now" (e.g.
# a symbol this account/region is permanently barred from trading) -- worth a
# much longer backoff than a transient failure like insufficient margin, which
# can resolve itself as soon as something else closes.
_PERMANENT_ENTRY_ERROR_CODES = ("110132",)  # regional restriction


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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

        self.email = EmailNotifier(
            smtp_host=cfg.secrets.email_smtp_host,
            smtp_port=cfg.secrets.email_smtp_port,
            smtp_user=cfg.secrets.email_smtp_user,
            smtp_password=cfg.secrets.email_smtp_password,
            email_from=cfg.secrets.email_from,
            email_to=cfg.secrets.email_to,
        )
        # _margin_for_new_position is called multiple times per entry attempt
        # (room check, then again when actually sizing) -- cache the exchange's
        # real available-balance lookup briefly so that doesn't mean multiple
        # extra API calls per tick.
        self._available_balance_cache: dict = {"ts": 0.0, "value": None}

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
        funding_rate = self.client.get_funding_rate(symbol)
        fund = funding.score(funding_rate, self.signals_cfg.get("funding", {}))
        weights = self.signals_cfg.get("weights", {})
        return aggregator.aggregate(tech, news, poly, weights, funding=fund)

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
    def _daily_loss_breached(self, equity: float) -> bool:
        limit = self.risk_cfg.get("max_daily_loss_pct", 8.0)
        return self.state.daily_loss_pct(equity) >= limit

    def _sync_daily_state(self, equity: float):
        """Keeps today's daily-loss-limit tracking accurate. The old behavior
        (state.ensure_daily) just reset realized_pnl to 0 whenever the local
        record didn't match today -- correct for a normal UTC day rollover, but
        on a state reset (a real risk on Render's free plan) it silently threw
        away whatever had actually been lost today, defeating the daily loss
        circuit breaker. Reconstructing "realized PnL since midnight UTC" from
        Bybit's own closed-pnl history is correct either way: a real rollover
        recovers 0 (nothing closed yet today) same as before, while a mid-day
        reset recovers the real number instead of assuming zero.
        """
        daily = self.state.snapshot().get("daily", {})
        if daily.get("start_equity") is not None and daily.get("date") == _today_utc():
            return  # already accurate for today

        realized_today = 0.0
        try:
            midnight = datetime.combine(datetime.now(timezone.utc).date(), datetime.min.time(), tzinfo=timezone.utc)
            records = self.client.get_closed_pnl_since(int(midnight.timestamp() * 1000))
            realized_today = sum(float(r.get("closedPnl") or 0) for r in records)
        except Exception:
            logger.exception("failed to reconstruct today's realized PnL from exchange -- starting at 0")

        start_equity = equity - realized_today
        self.state.seed_daily(_today_utc(), start_equity, realized_today)
        if realized_today != 0.0:
            logger.warning("reconstructed today's daily PnL from exchange history (state was reset): "
                           "realized=%.4f start_equity=%.4f", realized_today, start_equity)

    def _maybe_send_daily_summary(self):
        if not self.email.enabled:
            return
        today = _today_utc()
        if self.state.get_last_summary_date() == today:
            return

        daily = self.state.snapshot().get("daily", {})
        perf = compute_performance_summary(self.log_dir)
        overall = perf["overall"]
        win_rate = overall["win_rate"]
        lines = [
            f"날짜: {today}",
            f"오늘 실현손익: {daily.get('realized_pnl', 0.0):.4f} USDT",
            "",
            f"누적 거래: {overall['count']}건",
            f"누적 승률: {win_rate * 100:.0f}%" if win_rate is not None else "누적 승률: -",
            f"누적 손익: {overall['total_pnl']:.4f} USDT",
            "",
            f"추세추종: {perf['by_type']['trend']['count']}건, 손익 {perf['by_type']['trend']['total_pnl']:.4f} USDT",
            f"레인지 단타: {perf['by_type']['range']['count']}건, 손익 {perf['by_type']['range']['total_pnl']:.4f} USDT",
        ]
        for label, stats in sorted(perf["by_confidence"].items()):
            lines.append(f"신뢰도 {label}: {stats['count']}건, 승률 {stats['win_rate'] * 100:.0f}%, 손익 {stats['total_pnl']:.4f} USDT")

        self.email.send(f"[Bybit Bot] {today} 일일 요약", "\n".join(lines))
        self.state.set_last_summary_date(today)

    def try_enter(self, symbol: str):
        if self.state.get_trade(symbol) is not None:
            return

        backoff = self.state.get_entry_backoff(symbol)
        if backoff and time.time() < backoff.get("until_ts", 0):
            logger.debug("skip entry %s: in backoff (%s)", symbol, backoff.get("reason", ""))
            return

        equity = self.client.get_equity_usdt()
        self._sync_daily_state(equity)
        if self._daily_loss_breached(equity):
            return

        signal = self.get_signal(symbol)
        if signal["atr"] <= 0 or signal["close"] <= 0:
            return

        # Below this, the likely move over a trade's lifetime doesn't clearly
        # clear the round-trip taker fee -- entering is +EV noise at best. Skips
        # both trend and range entries; a dead/chopping market is dead for either.
        atr_pct = signal["atr"] / signal["close"] * 100.0
        min_atr_pct = self.risk_cfg.get("min_volatility_atr_pct", 0.0)
        if atr_pct < min_atr_pct:
            logger.debug("skip entry %s: volatility too low (ATR=%.4f%% < min %.4f%%)",
                         symbol, atr_pct, min_atr_pct)
            return

        min_conf = self.risk_cfg.get("min_confidence_to_enter", 0.55)
        is_trend_candidate = signal["direction"] != "neutral" and signal["confidence"] >= min_conf

        # Don't immediately re-open the same losing, going-nowhere trade right after
        # a stale_timeout close -- observed live as a symbol stuck in a tight range
        # getting shorted, stale-timed-out, and re-shorted in a fee-bleeding loop.
        # Only blocks a re-entry in the SAME direction; a genuine reversal (signal
        # now favors the other side) is unaffected.
        if is_trend_candidate:
            cooldown = self.state.get_stale_cooldown(symbol)
            if cooldown and cooldown.get("side") == signal["direction"] and time.time() < cooldown.get("until_ts", 0):
                logger.debug("skip entry %s: cooling down after a stale-timeout exit on the same side", symbol)
                return

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

    def _get_available_balance(self) -> float | None:
        now = time.time()
        cache = self._available_balance_cache
        if now - cache["ts"] < 10.0:
            return cache["value"]
        try:
            cache["value"] = self.client.get_available_balance_usdt()
        except BybitAPIError:
            cache["value"] = None
        cache["ts"] = now
        return cache["value"]

    def _margin_for_new_position(self, equity: float) -> float:
        """Target margin (position_size_pct_of_equity% of equity), capped so total
        margin in use never exceeds equity * (1 - margin_buffer_pct/100) -- i.e. a
        reserve is always kept free rather than every slot targeting its % of
        *total* equity independently and potentially over-committing.

        Also capped by the exchange's own real available balance if known: our
        local headroom estimate can drift from what Bybit actually allows
        (funding fees, price moves since our last snapshot, exchange-side
        margin requirements) -- observed live as repeated ErrCode 110007
        ("insufficient margin") rejections even with a margin buffer in place.
        """
        buffer_pct = self.risk_cfg.get("margin_buffer_pct", 15.0)
        headroom = equity * (1 - buffer_pct / 100.0) - self._used_margin()
        target = equity * self.risk_cfg.get("position_size_pct_of_equity", 25.0) / 100.0
        margin = max(0.0, min(target, headroom))

        available = self._get_available_balance()
        if available is not None:
            margin = max(0.0, min(margin, available))
        return margin

    def _has_capital_pressure(self) -> bool:
        """True if there's no room for a new position right now (every slot
        used, or margin headroom exhausted) -- i.e. recycling a stale
        position's capital would actually be useful. Used to decide how
        patient check_stale_position should be: no point rushing to pay a
        round-trip fee to free capital nothing is waiting on.
        """
        if self.state.open_trade_count() >= self.risk_cfg.get("max_concurrent_positions", 1):
            return True
        try:
            equity = self.client.get_equity_usdt()
        except BybitAPIError:
            return True  # can't tell -- default to the normal (less patient) timeout
        return self._margin_for_new_position(equity) <= 0

    def _entry_backoff_minutes(self, exc: Exception) -> float:
        if any(code in str(exc) for code in _PERMANENT_ENTRY_ERROR_CODES):
            return self.trade_cfg.get("permanent_error_backoff_min", 1440)
        return self.trade_cfg.get("entry_fail_backoff_min", 5)

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
            backoff_min = self._entry_backoff_minutes(exc)
            self.state.set_entry_backoff(symbol, time.time() + backoff_min * 60, reason=str(exc)[:200])
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
            backoff_min = self.trade_cfg.get("sl_tp_fail_backoff_min", 60)
            self.state.set_entry_backoff(symbol, time.time() + backoff_min * 60, reason="sl_tp_attach_failed")
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

        if reason == "stale_timeout":
            cooldown_min = self.trade_cfg.get("stale_reentry_cooldown_min", 30)
            if cooldown_min > 0:
                self.state.set_stale_cooldown(symbol, trade["side"], time.time() + cooldown_min * 60)

        self.state.add_realized_pnl(pnl)
        self.state.set_trade(symbol, None)
        # trade["pnl"] reported below is the WHOLE trade's realized pnl, including
        # any earlier partial take-profit leg -- that leg's pnl was already booked
        # into state via add_realized_pnl() when it happened, so it's not re-added
        # here, only folded into the number shown/recorded for this trade.
        total_pnl = pnl + trade.get("partial_realized_pnl", 0.0)
        self.state.record_closed_trade({
            "symbol": symbol, "side": trade["side"], "entry_price": trade["entry_price"],
            "exit_price": exit_price, "qty": trade["qty"], "pnl": total_pnl, "reason": reason,
            "pnl_is_estimate": pnl_is_estimate, "closed_at": time.time(),
        })

        est_tag = " (est.)" if pnl_is_estimate else ""
        msg = f"[종료:{reason}] {symbol} {trade['side'].upper()} exit~{exit_price:.4f} PnL={total_pnl:+.4f} USDT{est_tag}"
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "exit", "symbol": symbol, "reason": reason,
                                     "exit_price": exit_price, "pnl": total_pnl, "pnl_is_estimate": pnl_is_estimate})

    def _take_partial_profit(self, symbol: str, trade: dict, fraction: float):
        """Closes `fraction` of the position at market to lock in realized profit,
        leaving the rest open under the same SL and to keep riding trailing/TP
        extension. Skipped (not a full close) if either resulting leg would round
        below the exchange's min qty -- better to just keep riding the whole
        position than leave unclosable dust.
        """
        inst = self.client.get_instrument_info(symbol)
        partial_qty = self.client.round_qty(symbol, trade["qty"] * fraction)
        remaining_qty = self.client.round_qty(symbol, trade["qty"] - partial_qty)
        if partial_qty < inst.min_qty or remaining_qty < inst.min_qty:
            return

        try:
            self.client.close_position(symbol, trade["side"], partial_qty)
        except BybitAPIError as exc:
            logger.error("failed to take partial profit on %s: %s", symbol, exc)
            return

        closed = self.client.get_closed_pnl(symbol)
        if closed and closed["updated_time_ms"] / 1000.0 >= trade["opened_at"]:
            pnl = closed["closed_pnl"]
        else:
            try:
                price = self.client.get_last_price(symbol)
            except BybitAPIError:
                price = trade["entry_price"]
            if trade["side"] == "long":
                pnl = (price - trade["entry_price"]) * partial_qty
            else:
                pnl = (trade["entry_price"] - price) * partial_qty

        self.state.add_realized_pnl(pnl)
        trade["qty"] = remaining_qty
        trade["partial_tp_taken"] = True
        trade["partial_realized_pnl"] = trade.get("partial_realized_pnl", 0.0) + pnl
        self.state.set_trade(symbol, trade)

        msg = (f"[부분익절] {symbol} {trade['side'].upper()} {partial_qty} 청산 "
               f"(잔여 {remaining_qty}) PnL={pnl:+.4f} USDT")
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "partial_take_profit", "symbol": symbol,
                                     "qty": partial_qty, "remaining_qty": remaining_qty, "pnl": pnl})

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

        if stop_manager.check_stale_position(trade, price, self.trade_cfg, self._has_capital_pressure()):
            self._close_and_settle(symbol, trade, "stale_timeout")
            return

        if trade.get("is_range_trade"):
            # range/scalp trades exit only via their (tight) exchange SL/TP, the
            # shared flash-move/reversal/stale-timeout checks above -- no riding
            # the trade further with breakeven/trailing/TP-extension/partial-TP.
            self.state.set_trade(symbol, trade)
            return

        partial_cfg = self.trade_cfg.get("partial_tp", {})
        if partial_cfg.get("enabled", False) and not trade.get("partial_tp_taken", False):
            if stop_manager.profit_r(trade, price) >= partial_cfg.get("at_rr", 1.0):
                self._take_partial_profit(symbol, trade, partial_cfg.get("close_fraction", 0.5))
                trade = self.state.get_trade(symbol)
                if trade is None:  # shouldn't happen, but don't operate on a closed trade
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
            screened = universe.screen_top_symbols(
                self.client, quote_suffix="USDT", top_n=top_n,
                max_taker_fee_rate=self.universe_cfg.get("max_taker_fee_rate"),
            )
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

    def _reconcile_orphaned_positions(self):
        """Finds any position that's actually open on the exchange but isn't in
        local state (e.g. state.json was reset -- a real risk on Render's free
        plan -- or a position was opened outside the bot). Without this, such a
        position would never even be looked at: tick() only manages symbols in
        the watchlist or already-tracked state, so its SL/TP could sit unchecked
        indefinitely. Adopts it into state (keeping its existing SL/TP if any),
        verifying/repairing/closing exactly like a normal entry would.
        """
        try:
            open_positions = self.client.get_all_open_positions()
        except BybitAPIError:
            logger.exception("failed to reconcile open positions against local state")
            return

        tracked = set(self.state.snapshot().get("trades", {}).keys())
        for pos in open_positions:
            symbol = pos["symbol"]
            if symbol in tracked:
                continue

            logger.warning("found an open %s position not tracked locally -- adopting it", symbol)
            side = "long" if pos["side"] == "Buy" else "short"

            # We don't know this position's real entry-time ATR (we didn't open
            # it, or its state was lost) -- the current cached signal's ATR is
            # the best available stand-in for sizing a fresh SL/TP if needed.
            signal = self.get_signal(symbol)
            atr = signal.get("atr") or pos["entry_price"] * 0.01

            trade = stop_manager.new_trade(symbol, side, pos["entry_price"], pos["size"], atr, self.trade_cfg)
            trade["leverage"] = self.risk_cfg.get("max_leverage", 5)

            if pos["stop_loss"] > 0 and pos["take_profit"] > 0:
                trade["initial_sl"] = trade["current_sl"] = pos["stop_loss"]
                trade["initial_tp"] = trade["current_tp"] = pos["take_profit"]
            else:
                sl_price = self.client.round_price(symbol, trade["initial_sl"])
                tp_price = self.client.round_price(symbol, trade["initial_tp"])
                if not self._repair_sl_tp(symbol, sl_price, tp_price):
                    logger.critical("adopted %s has no SL/TP and repair failed -- closing for safety", symbol)
                    self.notifier.send(f"[긴급] 추적 안 되던 {symbol} 포지션의 SL/TP 설정 실패 - 청산 시도. 직접 확인하세요.")
                    try:
                        close_qty = self.client.round_qty(symbol, pos["size"])
                        self.client.close_position(symbol, side, close_qty)
                    except BybitAPIError:
                        logger.critical("COULD NOT CLOSE ORPHANED UNPROTECTED POSITION %s -- MANUAL ACTION REQUIRED", symbol)
                    continue
                trade["initial_sl"] = trade["current_sl"] = sl_price
                trade["initial_tp"] = trade["current_tp"] = tp_price

            self.state.set_trade(symbol, trade)
            self.notifier.send(
                f"[알림] 추적 안 되던 {symbol} 포지션을 발견해서 관리 대상으로 등록했습니다 "
                f"(SL={trade['current_sl']:.4f} TP={trade['current_tp']:.4f})."
            )
            log_decision(self.log_dir, {"event": "position_adopted", "symbol": symbol, "side": side,
                                         "entry_price": pos["entry_price"], "qty": pos["size"],
                                         "sl": trade["current_sl"], "tp": trade["current_tp"]})

    def tick(self):
        self._refresh_universe()
        self._reconcile_orphaned_positions()
        try:
            # Must run unconditionally every tick, not only from try_enter(): if
            # every watched symbol already has an open (e.g. just-adopted)
            # position, try_enter() is never called for any of them, and a
            # state reset's daily-loss reconstruction would otherwise never run
            # at all -- observed live after a Render restart left today's
            # realized-pnl stuck at 0 even though real losses had already
            # happened earlier that day.
            self._sync_daily_state(self.client.get_equity_usdt())
        except BybitAPIError:
            logger.exception("failed to sync daily state this tick")
        try:
            self._maybe_send_daily_summary()
        except Exception:
            logger.exception("failed to send daily summary email")

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
