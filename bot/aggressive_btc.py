"""Standalone single-symbol aggressive scalp strategy, currently pointed at
SOXLUSDT (switched from BTCUSDT -- see SYMBOL below).

Deliberately SEPARATE from bot/strategy.py (the main, conservative
multi-symbol system this whole codebase was built around) -- different risk
philosophy entirely, so it gets its own state file, own main loop, own entry
point (`python -m bot.aggressive_btc`), and never touches the main bot's
state or open positions. The two can run side by side without interfering.

SOXLUSDT is NOT a plain coin -- it's a tokenized 3x-leveraged semiconductor
ETF (the main bot's config.yaml excludes it from its own universe scan for
exactly this reason: realized PNL on this kind of embedded-leverage token has
been observed live running ~3x what entry/exit price * qty * leverage alone
would imply). Exchange leverage here is capped much lower than the original
BTC design (10x, not 24x) specifically to account for that -- see
MAX_LEVERAGE/LEVERAGE_HARD_CAP below. The TP/SL % math (ROI% = price_move% *
leverage) still assumes a plain linear instrument; it has NOT been verified
against SOXLUSDT's actual embedded-leverage PNL behavior, so the realized
ROI% at TP/SL may still diverge from the configured 12%/-5% target more than
it does for BTCUSDT.

Design, as specified:
  1. 1-hour candles, target +12% ROI (on margin) per trade.
  2. Both long and short entries, whichever direction the blended signal favors.
  3. Entry signal: technical (EMA/RSI/MACD/Bollinger + volume) blended with
     Polymarket and general sector news, all computed off 1h candles /
     latest headlines for SYMBOL. Polymarket has no SOXLUSDT-relevant
     markets (see POLYMARKET_CFG below) -- it always contributes 0 for this
     symbol, same as for any non-BTC/ETH symbol in the main bot. News (see
     NEWS_CFG) is semiconductor-sector-specific RSS feeds + an optional
     NewsAPI query, scored the same way as the main bot's crypto news signal.
  4. Stop-loss at -5% ROI.
  5. Every full hour the position stays open, if its ROI is still negative,
     close it rather than let it drift indefinitely.
  6. 50% of equity used as margin per trade.
  7. Leverage auto-sized so the 12% target is roughly reachable within an
     hour given the symbol's recent realized volatility -- clipped to
     [6x, 10x], hard-capped at 10x no matter how low volatility reads (the
     real safety backstop: a calm stretch must never push leverage past
     this, since a sudden move hits hardest exactly when leverage was
     raised to compensate for the apparent calm -- lowered from BTC's
     24x/25x specifically because SOXLUSDT already carries embedded 3x
     leverage from the underlying ETF).

================================================================================
TESTNET below is the ONLY thing that needs to change to go live. Defaults to
True. Flipping it to False points this module at the real account -- nothing
else about its risk profile (the 10x cap, -5% SL, hourly loss-cutoff, 50%
margin-per-trade) changes between test and live; going live only changes
which account's money is at risk.
================================================================================
"""
from __future__ import annotations

import logging
import os
import time

from bot.config import Secrets, load_config
from bot.exchange.bybit_client import BybitAPIError, BybitClient
from bot.logger import log_decision
from bot.notify import Notifier
from bot.risk.position_sizing import compute_qty_fixed_margin
from bot.signals import technical
from bot.signals.news import NewsSignal
from bot.signals.polymarket import PolymarketSignal
from bot.state import StateStore

logger = logging.getLogger("bot.aggressive_btc")

# ==============================================================================
TESTNET = False   # <-- flip this one line to go live. See module docstring.
# ==============================================================================

SYMBOL = "SOXLUSDT"             # switched from BTCUSDT -- see module docstring
TIMEFRAME = "60"                # Bybit kline interval string for 1-hour candles
TARGET_PCT = 12.0               # take-profit, % ROI on margin
STOP_LOSS_PCT = 5.0             # stop-loss, % ROI on margin
HOURLY_CUTOFF_PCT = 0.0         # at each full hour open, exit if ROI <= this
MARGIN_PCT_OF_EQUITY = 50.0     # % of equity used as margin per trade
MIN_LEVERAGE = 6.0
MAX_LEVERAGE = 10.0             # lowered from 24.0: SOXLUSDT already carries embedded 3x leverage (see module docstring)
LEVERAGE_HARD_CAP = 10.0        # lowered from 25.0, same reason -- never exceeded regardless of volatility
MIN_CONFIDENCE_TO_ENTER = 0.4   # |blended score| needed to call it "강세"
# Lowered from 0.5: at 0.5, 1h BTCUSDT rarely produced a strong enough blended
# score to enter at all (observed live: a +0.34 blend sat under the bar for
# hours) -- 0.4 trades more often while still requiring a real move, not
# just noise-level confirmation, from whichever components are active.
POLL_SEC = 30
MIN_ORDER_NOTIONAL_USDT = 5.0
# Logging every 30s tick's signal to decisions.jsonl would mostly be
# unchanged noise (ticks that often show the same "still neutral" read) --
# throttled to once per 5 minutes gives a readable score-over-time history on
# the dashboard's "최근 판단 로그" without flooding the (ephemeral, redeploy-
# wiped) log file.
SIGNAL_LOG_INTERVAL_SEC = 300

TECH_CFG = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "atr_period": 14}

POLYMARKET_CFG = {
    "keywords": ["bitcoin", "crypto", "fed ", "interest rate", "recession"],
    "refresh_minutes": 15,
    "max_markets": 40,
    # No markets here are about semiconductors/SOXL -- leaving SYMBOL out of
    # relevant_symbols makes PolymarketSignal.score always return 0
    # score/confidence for it, same as any non-BTC/ETH symbol in the main
    # bot, instead of letting unrelated bitcoin/macro sentiment push this
    # signal's direction.
    "relevant_symbols": [],
}

# Unlike Polymarket (no semiconductor-relevant prediction markets exist),
# general news is genuinely informative for SOXLUSDT -- sector-moving events
# (export-control changes, a major chipmaker's earnings, Fed rate decisions)
# show up in headlines well before they fully show up in 1h technical
# indicators. Free RSS only (no mandatory paid dependency), same pattern as
# the main bot's news signal; NEWSAPI_KEY (if set) supplements these with a
# semiconductor-specific query instead of the main bot's crypto one.
NEWS_CFG = {
    "feeds": [
        "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # CNBC Technology
        "https://www.investing.com/rss/news_25.rss",              # Investing.com stock market news
        "https://www.marketwatch.com/rss/topstories",             # MarketWatch top stories
    ],
    "newsapi_query": "semiconductor OR chip OR chips OR Nvidia OR AMD OR TSMC OR Intel",
    "refresh_minutes": 15,
    "lookback_hours": 12,
    "max_articles": 60,
}


def state_path() -> str:
    # Own file, never shared with the main bot's data/state.json.
    return os.path.join(os.getenv("DATA_DIR", "data"), "aggressive_btc_state.json")


def compute_leverage(atr_pct: float, target_pct: float = TARGET_PCT,
                      min_leverage: float = MIN_LEVERAGE, max_leverage: float = MAX_LEVERAGE,
                      hard_cap: float = LEVERAGE_HARD_CAP) -> float:
    """Sizes leverage so `target_pct` ROI is roughly reachable given the
    symbol's recent realized volatility (atr_pct, as a % of price, on the
    1h timeframe) -- a smaller recent move needs more leverage to turn into
    the same ROI%, a larger one needs less. Clipped to [min_leverage,
    max_leverage], then hard-capped at `hard_cap` regardless of how low
    atr_pct reads -- the backstop against the "calmer market -> higher
    leverage" feedback loop raising risk exactly when a surprise move would
    hurt most.
    """
    if atr_pct <= 0:
        return min_leverage
    required = target_pct / atr_pct
    return min(hard_cap, max(min_leverage, min(max_leverage, required)))


def compute_tp_sl_prices(entry_price: float, side: str, leverage: float,
                          target_pct: float = TARGET_PCT, stop_loss_pct: float = STOP_LOSS_PCT) -> dict:
    """TP/SL prices implied by a target/stop expressed as % ROI on margin --
    ROI% ~= price_move_pct * leverage, so price_move_pct = ROI% / leverage.
    """
    target_move = (target_pct / 100.0) / leverage
    stop_move = (stop_loss_pct / 100.0) / leverage
    if side == "long":
        return {"tp": entry_price * (1 + target_move), "sl": entry_price * (1 - stop_move)}
    return {"tp": entry_price * (1 - target_move), "sl": entry_price * (1 + stop_move)}


def compute_signal(technical_result: dict, volume_score: float, poly_result: dict,
                    news_result: dict | None = None) -> dict:
    """Blends the 1h technical score (EMA trend + RSI momentum + MACD +
    Bollinger, from technical.score_timeframe) with volume confirmation,
    Polymarket sentiment (currently always 0 for SOXLUSDT -- see
    POLYMARKET_CFG's empty relevant_symbols), and general sector news into
    one directional call -- both long and short are eligible, whichever the
    blend favors. Polymarket and news each only count when they actually
    found something relevant (their own confidence), same pattern as the
    main bot.

    Weights: technical 0.10, volume 0.15, polymarket 0.10, news 0.65.
    Polymarket stays low (and inert for SOXLUSDT): its crypto-relevant
    markets have nothing to do with semiconductors. News now dominates the
    blend -- sector-moving events (export-control changes, a major
    chipmaker's earnings, Fed rate decisions) tend to show up in headlines
    before they fully show up in 1h technical indicators, unlike
    Polymarket's weeks-to-months-out questions. Note that news_conf is
    capped at relevant_article_count/15 (see NewsSignal.score_for_symbol),
    so this 0.65 weight is rarely fully realized -- with only 1-2 relevant
    articles (confidence ~0.07-0.13, as observed live), news's actual
    contribution is a small fraction of 0.65 even on strongly one-sided
    headlines, and technical (now only 0.10) has correspondingly little
    power to confirm or override it.
    """
    news_result = news_result or {}
    tech_score = technical_result.get("score", 0.0)
    poly_score = poly_result.get("score", 0.0)
    poly_conf = poly_result.get("confidence", 0.0)
    news_score = news_result.get("score", 0.0)
    news_conf = news_result.get("confidence", 0.0)
    news_bullish = news_result.get("bullish_count", 0)
    news_bearish = news_result.get("bearish_count", 0)
    news_neutral = news_result.get("neutral_count", 0)

    blended = (0.10 * tech_score + 0.15 * volume_score
               + 0.10 * (poly_score * poly_conf) + 0.65 * (news_score * news_conf))
    blended = max(-1.0, min(1.0, blended))

    if blended >= MIN_CONFIDENCE_TO_ENTER:
        direction = "long"
    elif blended <= -MIN_CONFIDENCE_TO_ENTER:
        direction = "short"
    else:
        direction = "neutral"

    return {"direction": direction, "score": blended, "atr": technical_result.get("atr", 0.0),
            "close": technical_result.get("close", 0.0),
            "components": {"technical": tech_score, "volume": volume_score,
                            "polymarket": poly_score, "polymarket_confidence": poly_conf,
                            "news": news_score, "news_confidence": news_conf,
                            "news_bullish": news_bullish, "news_bearish": news_bearish,
                            "news_neutral": news_neutral}}


def should_hourly_cutoff(hours_open: float, roi_pct: float, last_hour_checked: int) -> tuple[bool, int]:
    """True (and the hour-mark that triggered it) if the position has just
    crossed a new full-hour mark since open AND its ROI is still <=
    HOURLY_CUTOFF_PCT at that moment -- checked once per hour boundary, not
    continuously, so a trade already green at hour 1 isn't re-punished for a
    dip at hour 1.5. Returns (should_close, updated_last_hour_checked) --
    callers persist the second value on the trade even when not closing, so
    the SAME hour mark isn't evaluated twice.
    """
    current_mark = int(hours_open)
    if current_mark <= last_hour_checked or current_mark < 1:
        return False, last_hour_checked
    return (roi_pct <= HOURLY_CUTOFF_PCT), current_mark


def roi_pct(unrealized_pnl: float, margin: float) -> float:
    if margin <= 0:
        return 0.0
    return unrealized_pnl / margin * 100.0


class AggressiveBTCStrategy:
    def __init__(self, client: BybitClient, state: StateStore, notifier: Notifier,
                 poly_signal: PolymarketSignal, news_signal: NewsSignal, log_dir: str = "logs"):
        self.client = client
        self.state = state
        self.notifier = notifier
        self.poly_signal = poly_signal
        self.news_signal = news_signal
        self.log_dir = log_dir
        self._last_signal_log_ts = 0.0

    # -- signal ---------------------------------------------------------
    def get_signal(self) -> dict:
        candles = self.client.get_klines(SYMBOL, TIMEFRAME, limit=200)
        tech_result = technical.score_timeframe(candles, TECH_CFG)
        vol_score = technical.volume_score(candles)
        poly_result = self.poly_signal.score(SYMBOL)
        news_result = self.news_signal.score_for_symbol(SYMBOL)
        return compute_signal(tech_result, vol_score, poly_result, news_result)

    # -- entry ---------------------------------------------------------
    def _format_signal_breakdown(self, signal: dict) -> str:
        c = signal.get("components", {})
        return (f"T:{c.get('technical', 0.0):+.2f} V:{c.get('volume', 0.0):+.2f} "
                f"P:{c.get('polymarket', 0.0):+.2f}(conf {c.get('polymarket_confidence', 0.0):.2f}) "
                f"N:{c.get('news', 0.0):+.2f}(conf {c.get('news_confidence', 0.0):.2f}, "
                f"긍정{c.get('news_bullish', 0)}/부정{c.get('news_bearish', 0)}/중립{c.get('news_neutral', 0)}) "
                f"-> blended={signal['score']:+.2f}")

    @staticmethod
    def _signal_for_log(signal: dict) -> dict:
        """Reshapes compute_signal()'s flat components dict (e.g.
        {"technical": 0.41, ...}) into the nested-per-source shape the
        dashboard's _component_breakdown (built for the main bot's
        aggregator.py output) expects (e.g. {"technical": {"score": 0.41}}),
        so the same "최근 판단 로그" rendering works for both without
        duplicating that formatting logic here.
        """
        c = signal.get("components", {})
        return {"components": {k: {"score": c[k]} for k in ("technical", "volume", "polymarket", "news") if k in c}}

    def _log_signal_check(self, signal: dict):
        """Throttled record of the blended score even while just sitting flat
        in "관망" -- without this, decisions.jsonl (and the dashboard's "최근
        판단 로그") only ever shows actual entries/exits, which can be hours
        apart, with no way to see how the score was trending in between.
        """
        now = time.time()
        if now - self._last_signal_log_ts < SIGNAL_LOG_INTERVAL_SEC:
            return
        self._last_signal_log_ts = now
        log_decision(self.log_dir, {
            "event": "signal_check", "symbol": SYMBOL, "direction": signal.get("direction"),
            "score": signal.get("score"), "signal": self._signal_for_log(signal),
        })

    def try_enter(self):
        if self.state.get_trade(SYMBOL) is not None:
            return

        signal = self.get_signal()
        self._log_signal_check(signal)
        if signal["atr"] <= 0 or signal["close"] <= 0:
            self.state.set_skip_reason(SYMBOL, "가격/ATR 데이터 없음")
            return
        if signal["direction"] == "neutral":
            self.state.set_skip_reason(
                SYMBOL, f"관망 중 - 신호 약함 ({self._format_signal_breakdown(signal)}, "
                        f"기준 ±{MIN_CONFIDENCE_TO_ENTER:.2f})")
            return

        equity = self.client.get_equity_usdt()
        margin = equity * (MARGIN_PCT_OF_EQUITY / 100.0)
        available = self.client.get_available_balance_usdt()
        if available is not None:
            margin = min(margin, available)
        if margin <= 0:
            self.state.set_skip_reason(SYMBOL, "여유 증거금 없음")
            return

        atr_pct = signal["atr"] / signal["close"] * 100.0
        leverage = compute_leverage(atr_pct)

        inst = self.client.get_instrument_info(SYMBOL)
        leverage = min(leverage, inst.max_leverage)
        # BybitClient.set_leverage truncates to a whole number when it talks to
        # the exchange -- round down here too, BEFORE computing TP/SL prices,
        # so the price targets are derived from the SAME leverage that actually
        # ends up applied to the position. Otherwise a fractional leverage here
        # (e.g. 21.6x) vs an integer 21x on the exchange silently shifts the
        # realized ROI% at TP/SL away from the intended 12%/-5%.
        leverage = float(int(leverage))

        side = signal["direction"]
        entry_price = self.client.get_last_price(SYMBOL)
        prices = compute_tp_sl_prices(entry_price, side, leverage)

        sizing = compute_qty_fixed_margin(
            margin=margin, leverage=leverage, entry_price=entry_price,
            qty_step=inst.qty_step, min_qty=inst.min_qty, min_notional=MIN_ORDER_NOTIONAL_USDT,
        )
        if not sizing.ok:
            # MARGIN_PCT_OF_EQUITY of equity alone doesn't clear the exchange's
            # min qty / the configured min notional at this leverage -- retry
            # with just enough margin to clear it (never more than what's
            # actually available) instead of skipping the trade outright.
            required_notional = max(MIN_ORDER_NOTIONAL_USDT, inst.min_qty * entry_price)
            required_notional += inst.qty_step * entry_price  # survive compute_qty_fixed_margin's floor-down rounding
            min_margin_needed = (required_notional / leverage) * 1.01  # small pad for float rounding
            available_for_min = available if available is not None else equity
            if min_margin_needed <= available_for_min:
                margin = min_margin_needed
                sizing = compute_qty_fixed_margin(
                    margin=margin, leverage=leverage, entry_price=entry_price,
                    qty_step=inst.qty_step, min_qty=inst.min_qty, min_notional=MIN_ORDER_NOTIONAL_USDT,
                )
            if not sizing.ok:
                logger.info("aggressive_btc: skip entry, %s", sizing.reason)
                self.state.set_skip_reason(SYMBOL, f"주문 크기 조건 미충족 ({sizing.reason})")
                return

        qty = self.client.round_qty(SYMBOL, sizing.qty)
        sl_price = self.client.round_price(SYMBOL, prices["sl"])
        tp_price = self.client.round_price(SYMBOL, prices["tp"])

        try:
            self.client.set_leverage(SYMBOL, leverage)
            self.client.open_position(SYMBOL, side, qty, stop_loss=sl_price, take_profit=tp_price)
        except BybitAPIError as exc:
            logger.error("aggressive_btc: failed to open position: %s", exc)
            self.state.set_skip_reason(SYMBOL, f"주문 실패 ({exc})")
            return

        # Same safety-first pattern as the main bot: verify SL/TP actually
        # attached (Bybit can silently drop them even when the base order
        # fills) -- close immediately rather than ever hold an unprotected
        # leveraged position.
        exchange_position = self.client.get_position(SYMBOL)
        attached = bool(exchange_position) and exchange_position["stop_loss"] > 0 and exchange_position["take_profit"] > 0
        if not attached:
            logger.critical("aggressive_btc: %s opened with no protective SL/TP -- closing immediately", SYMBOL)
            try:
                self.client.close_position(SYMBOL, side, qty)
            except BybitAPIError as exc:
                logger.critical("aggressive_btc: COULD NOT CLOSE UNPROTECTED POSITION -- MANUAL ACTION REQUIRED: %s", exc)
            self.notifier.send(f"[긴급] aggressive_btc: {SYMBOL} SL/TP 설정 실패 - 긴급 청산 시도. Bybit 앱에서 직접 확인하세요.")
            self.state.set_skip_reason(SYMBOL, "SL/TP 설정 실패로 긴급 청산됨")
            return

        trade = {
            "symbol": SYMBOL, "side": side, "entry_price": entry_price, "qty": qty,
            "margin": margin, "leverage": leverage, "current_sl": sl_price, "current_tp": tp_price,
            "opened_at": time.time(), "last_hour_checked": 0,
        }
        self.state.set_trade(SYMBOL, trade)
        self.state.clear_skip_reason(SYMBOL)
        msg = (f"[aggressive_btc 진입] {SYMBOL} {side.upper()} qty={qty} entry~{entry_price:.2f} "
               f"SL={sl_price:.2f} TP={tp_price:.2f} lev={leverage:.1f}x score={signal['score']:+.2f}")
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "entry", "symbol": SYMBOL, "side": side, "qty": qty,
                                     "entry_price": entry_price, "sl": sl_price, "tp": tp_price,
                                     "leverage": leverage, "signal": self._signal_for_log(signal)})

    # -- management ---------------------------------------------------------
    def manage_open_position(self):
        trade = self.state.get_trade(SYMBOL)
        if trade is None:
            return

        exchange_position = self.client.get_position(SYMBOL)
        if exchange_position is None:
            # SL or TP already fired on the exchange side -- nothing left to
            # close, just settle locally.
            self._close_and_settle(trade, "sl_tp_hit", already_closed=True)
            return

        hours_open = (time.time() - trade["opened_at"]) / 3600.0
        current_roi = roi_pct(exchange_position["unrealized_pnl"], trade["margin"])
        should_close, new_mark = should_hourly_cutoff(hours_open, current_roi, trade["last_hour_checked"])
        trade["last_hour_checked"] = new_mark
        if should_close:
            logger.info("aggressive_btc: hourly cutoff at hour %d, ROI=%.2f%% still negative -- closing", new_mark, current_roi)
            self._close_and_settle(trade, "hourly_cutoff")
            return

        self.state.set_trade(SYMBOL, trade)

    def _close_and_settle(self, trade: dict, reason: str, already_closed: bool = False):
        if not already_closed:
            try:
                close_qty = self.client.round_qty(SYMBOL, trade["qty"])
                self.client.close_position(SYMBOL, trade["side"], close_qty)
            except BybitAPIError as exc:
                logger.error("aggressive_btc: failed to close %s: %s", SYMBOL, exc)
                return

        closed = self.client.get_closed_pnl(SYMBOL)
        if closed and closed["updated_time_ms"] / 1000.0 >= trade["opened_at"]:
            pnl = closed["closed_pnl"]
            pnl_is_estimate = False
        else:
            try:
                exit_price = self.client.get_last_price(SYMBOL)
            except BybitAPIError:
                exit_price = trade["entry_price"]
            if trade["side"] == "long":
                pnl = (exit_price - trade["entry_price"]) * trade["qty"]
            else:
                pnl = (trade["entry_price"] - exit_price) * trade["qty"]
            pnl_is_estimate = True

        self.state.set_trade(SYMBOL, None)
        self.state.record_closed_trade({
            "symbol": SYMBOL, "side": trade["side"], "entry_price": trade["entry_price"],
            "qty": trade["qty"], "pnl": pnl, "reason": reason, "pnl_is_estimate": pnl_is_estimate,
            "leverage": trade["leverage"], "margin": trade["margin"], "closed_at": time.time(),
        })
        est_tag = " (est.)" if pnl_is_estimate else ""
        msg = f"[aggressive_btc 종료:{reason}] {SYMBOL} {trade['side'].upper()} PnL={pnl:+.4f} USDT{est_tag}"
        logger.info(msg)
        self.notifier.send(msg)
        log_decision(self.log_dir, {"event": "exit", "symbol": SYMBOL, "side": trade["side"],
                                     "reason": reason, "pnl": pnl, "pnl_is_estimate": pnl_is_estimate})

    # -- loop ---------------------------------------------------------
    def tick(self):
        if self.state.get_trade(SYMBOL) is not None:
            self.manage_open_position()
        else:
            self.try_enter()


def _resolve_credentials(secrets: Secrets) -> tuple[str, str] | None:
    """Bybit testnet and live are separate accounts with separate API keys --
    a live-account key will NOT authenticate against the testnet host. Going
    live reuses the main bot's own BYBIT_API_KEY/SECRET (same real account);
    testnet needs its own dedicated AGGRESSIVE_BTC_TESTNET_API_KEY/SECRET
    from a Bybit testnet account, set separately in .env (see .env.example).
    Returns None (caller should refuse to start) if whichever pair this mode
    needs isn't actually configured.
    """
    if not TESTNET:
        return secrets.bybit_api_key, secrets.bybit_api_secret
    key = os.getenv("AGGRESSIVE_BTC_TESTNET_API_KEY", "").strip()
    secret = os.getenv("AGGRESSIVE_BTC_TESTNET_API_SECRET", "").strip()
    if not key or not secret:
        return None
    return key, secret


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    secrets: Secrets = cfg.secrets

    mode = "TESTNET (모의투자)" if TESTNET else "LIVE (실거래, 실제 자금 사용)"
    logger.warning("aggressive_btc starting | mode=%s | symbol=%s | target=+%.0f%% SL=-%.0f%% "
                    "leverage=[%.0f-%.0f]x cap=%.0fx margin=%.0f%%",
                    mode, SYMBOL, TARGET_PCT, STOP_LOSS_PCT, MIN_LEVERAGE, MAX_LEVERAGE,
                    LEVERAGE_HARD_CAP, MARGIN_PCT_OF_EQUITY)

    credentials = _resolve_credentials(secrets)
    notifier = Notifier(secrets.telegram_bot_token, secrets.telegram_chat_id)
    if credentials is None:
        msg = ("aggressive_btc: AGGRESSIVE_BTC_TESTNET_API_KEY/SECRET not set -- "
               "refusing to start in TESTNET mode without dedicated testnet credentials "
               "(a live-account key won't authenticate against the testnet host). "
               "See .env.example.")
        logger.critical(msg)
        notifier.send(f"[aggressive_btc] 시작 실패: {msg}")
        return
    api_key, api_secret = credentials

    client = BybitClient(api_key=api_key, api_secret=api_secret, testnet=TESTNET, category="linear")
    state = StateStore(state_path())
    poly_signal = PolymarketSignal(POLYMARKET_CFG)
    news_signal = NewsSignal(NEWS_CFG, secrets.newsapi_key)
    log_dir = os.getenv("LOG_DIR", "logs")  # same shared decisions.jsonl the main bot and dashboard use
    strategy = AggressiveBTCStrategy(client, state, notifier, poly_signal, news_signal, log_dir)

    notifier.send(f"aggressive_btc 시작됨 - {mode} - {SYMBOL} 단독, 목표 +{TARGET_PCT:.0f}% / 손절 -{STOP_LOSS_PCT:.0f}%")

    while True:
        loop_start = time.time()
        try:
            strategy.tick()
        except Exception:
            logger.exception("aggressive_btc: unhandled error in main loop tick")
        elapsed = time.time() - loop_start
        time.sleep(max(1.0, POLL_SEC - elapsed))


if __name__ == "__main__":
    main()
