"""Standalone aggressive scalp strategy, run independently per symbol (see
AGGRESSIVE_CONFIGS below: SOXLUSDT plus 38 non-leveraged tokenized US stocks,
split across two themed news pools -- SEMI_AI_CONFIGS for semiconductor/
AI-infrastructure names, STOCK_CONFIGS for everything else).

Deliberately SEPARATE from bot/strategy.py (the main, conservative
multi-symbol system this whole codebase was built around) -- different risk
philosophy entirely, so it gets its own state file, own main loop, own entry
point (`python -m bot.aggressive_btc`), and never touches the main bot's
state or open positions. The two can run side by side without interfering.
All configured symbols share this one state file (StateStore's trades/
skip_reasons are already keyed by symbol) and one Bybit client/notifier;
each gets its own SymbolConfig (leverage range, weights, etc), but
PolymarketSignal/NewsSignal instances are shared across symbols whose cfg
dicts are identical (see main()'s _signal_cache) so the ~20 symbols in each
stock news pool don't each independently re-fetch the same RSS feeds.

SOXLUSDT is NOT a plain coin -- it's a tokenized 3x leveraged semiconductor-
index ETF. The main bot's config.yaml excludes it from its own universe scan
for exactly this reason: realized PNL on this kind of embedded-leverage
token has been observed live diverging from what entry/exit price * qty *
leverage alone would imply (a related tokenized ETF, SNXXUSDT, showed a 2.4%
price move closing at +48% PNL on a ~7x position vs. the ~16.8% plain math
would predict -- SNXXUSDT was tried here and then removed from this module
for exactly that unpredictability). The 38 stock symbols below are
DELIBERATELY NOT this kind of product -- they're Bybit's 1x tokenized-stock
perpetuals (e.g. AAPLUSDT tracks Apple's share price directly, no embedded
multiplier), chosen specifically to avoid the embedded-leverage risk that
made SNXXUSDT's behavior unreliable. The first 10 were confirmed directly
against screenshots of Bybit's own listing; the other 28 were not (this
sandboxed session can't reach api.bybit.com itself to verify) -- see
main()'s startup validation, which queries get_instrument_info() for every
configured symbol and skips (with a logged warning + notifier message,
rather than crashing) any ticker that turns out not to exist or not be
tradable. Check the dashboard after a deploy to see which actually came up;
any missing one needs its ticker corrected against the Bybit app.

Because all 39 symbols independently look for entry signals every tick, this
module enforces a GLOBAL cap of exactly one open position at a time across
all of them (see try_enter()'s check of state.open_trade_count()) -- a
deliberate choice (over e.g. letting several symbols size down to minimum
margin at once) so the existing 50%-of-equity-per-trade sizing never has to
be shared or diluted across concurrent positions. With 39 symbols each doing
its own kline fetch every tick (POLL_SEC=30s), one tick loop iteration now
makes ~39 sequential Bybit API calls before sleeping -- still fine against
Bybit's public rate limits and POLL_SEC's own headroom, but worth knowing if
the loop is ever observed falling behind real time.

Design, as specified (per symbol, via SymbolConfig):
  1. 1-hour candles, target +12% ROI (on margin) per trade.
  2. Both long and short entries, whichever direction the blended signal favors.
  3. Entry signal: technical (EMA/RSI/MACD/Bollinger + volume) blended with
     Polymarket and general sector/company news, all computed off 1h
     candles / latest headlines for the symbol. Polymarket has no relevant
     markets for any of these symbols (see polymarket_cfg below) -- it
     always contributes 0, same as for any non-BTC/ETH symbol in the main
     bot. News (see news_cfg) is RSS feeds + an optional NewsAPI query,
     filtered per-symbol via news.SYMBOL_KEYWORDS, scored the same way as
     the main bot's crypto news signal.
  4. Stop-loss at -5% ROI.
  5. Every full hour the position stays open, if its ROI is still negative,
     close it rather than let it drift indefinitely.
  6. 50% of equity used as margin per trade (safe here specifically because
     of the global one-position-at-a-time cap above -- it was NOT safe to
     also give every stock symbol 50% without that cap, since several could
     otherwise try to commit 50% each at once).
  7. Leverage auto-sized so the 12% target is roughly reachable within an
     hour given the symbol's recent realized volatility -- clipped to
     [min_leverage, max_leverage], hard-capped at leverage_hard_cap no
     matter how low volatility reads (the real safety backstop: a calm
     stretch must never push leverage past this, since a sudden move hits
     hardest exactly when leverage was raised to compensate for the
     apparent calm). SOXLUSDT: [6x, 10x] (lowered from the original BTC
     design's 24x/25x because it carries embedded 3x leverage). All 38 stock
     symbols: [8x, 15x] -- higher than SOXLUSDT since there's no embedded
     multiplier distorting the TP/SL math, but still well under the original
     24x/25x crypto design since this is a brand-new asset class for this
     bot and real stocks can gap sharply on earnings/news.

================================================================================
TESTNET below is the ONLY thing that needs to change to go live. Defaults to
True. Flipping it to False points this module at the real account -- nothing
else about its risk profile (the per-symbol leverage caps, -5% SL, hourly
loss-cutoff, 50% margin-per-trade) changes between test and live; going live
only changes which account's money is at risk.
================================================================================
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

# Top-level module (repo root, not inside bot/) so it's the one file you open
# to retune entry sensitivity without digging through strategy code.
from aggressive_btc_settings import ENTRY_THRESHOLD
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

# -- shared across every configured symbol ---------------------------------------------------------
TIMEFRAME = "60"                # Bybit kline interval string for 1-hour candles
HOURLY_CUTOFF_PCT = 0.0         # at each full hour open, exit if ROI <= this
POLL_SEC = 30
MIN_ORDER_NOTIONAL_USDT = 5.0
# Logging every 30s tick's signal to decisions.jsonl would mostly be
# unchanged noise (ticks that often show the same "still neutral" read) --
# throttled to once per 5 minutes gives a readable score-over-time history on
# the dashboard's "최근 판단 로그" without flooding the (ephemeral, redeploy-
# wiped) log file.
SIGNAL_LOG_INTERVAL_SEC = 300

TECH_CFG = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "atr_period": 14}


@dataclass
class SymbolConfig:
    """Per-symbol knobs. 39 instances of this (SOXL_CONFIG plus every
    SEMI_AI_CONFIGS/STOCK_CONFIGS entry, built by _make_stock_config) each
    drive one AggressiveBTCStrategy; everything else about the strategy's
    mechanics (hourly cutoff, poll interval, TP/SL formula) is shared via the
    module-level constants above.
    """
    symbol: str
    target_pct: float = 12.0               # take-profit, % ROI on margin
    stop_loss_pct: float = 5.0             # stop-loss, % ROI on margin
    margin_pct_of_equity: float = 50.0     # % of equity used as margin per trade
    min_leverage: float = 6.0
    max_leverage: float = 10.0
    leverage_hard_cap: float = 10.0        # never exceeded regardless of volatility
    min_confidence_to_enter: float = ENTRY_THRESHOLD   # |blended score| needed to call it "강세"
    tech_weight: float = 0.10
    volume_weight: float = 0.15
    poly_weight: float = 0.10
    news_weight: float = 0.65
    tech_cfg: dict = field(default_factory=lambda: dict(TECH_CFG))
    polymarket_cfg: dict = field(default_factory=dict)
    news_cfg: dict = field(default_factory=dict)


SOXL_CONFIG = SymbolConfig(
    symbol="SOXLUSDT",
    min_leverage=6.0, max_leverage=10.0, leverage_hard_cap=10.0,
    min_confidence_to_enter=ENTRY_THRESHOLD,
    polymarket_cfg={
        "keywords": ["bitcoin", "crypto", "fed ", "interest rate", "recession"],
        "refresh_minutes": 15,
        "max_markets": 40,
        # No markets here are about semiconductors/SOXL -- leaving the
        # symbol out of relevant_symbols makes PolymarketSignal.score always
        # return 0 score/confidence for it, same as any non-BTC/ETH symbol
        # in the main bot, instead of letting unrelated bitcoin/macro
        # sentiment push this signal's direction.
        "relevant_symbols": [],
    },
    # General news is genuinely informative for SOXLUSDT (unlike Polymarket)
    # -- sector-moving events (export-control changes, a major chipmaker's
    # earnings, Fed rate decisions) show up in headlines well before they
    # fully show up in 1h technical indicators. Free RSS only (no mandatory
    # paid dependency); NEWSAPI_KEY (if set) supplements these with a
    # semiconductor-specific query instead of the main bot's crypto one.
    news_cfg={
        "feeds": [
            "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # CNBC Technology
            "https://www.investing.com/rss/news_25.rss",              # Investing.com stock market news
            "https://www.marketwatch.com/rss/topstories",             # MarketWatch top stories
            "https://www.nasdaq.com/feed/rssoutbound?category=Semiconductors",  # Nasdaq Semiconductors category
            "https://www.benzinga.com/feed",                          # Benzinga market news
            "https://finance.yahoo.com/news/rssindex",                # Yahoo Finance
            # Google News RSS search -- already pre-filtered to this exact
            # query, so it should skew more relevant per-article than the
            # general finance feeds above.
            "https://news.google.com/rss/search?q=semiconductor+OR+chip+OR+Nvidia+OR+AMD+OR+TSMC&hl=en-US&gl=US&ceid=US:en",
            "https://techcrunch.com/feed/",                           # TechCrunch
            "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC Markets
            "https://www.zdnet.com/news/rss.xml",                     # ZDNet
        ],
        "newsapi_query": "semiconductor OR chip OR chips OR Nvidia OR AMD OR TSMC OR Intel",
        "refresh_minutes": 15,
        "lookback_hours": 12,
        "max_articles": 60,
    },
)

# Shared by every stock SymbolConfig below (same object, not just same
# content) so main()'s signal-instance cache gives them all ONE NewsSignal/
# PolymarketSignal instance -- every symbol in a pool asking "what's relevant
# to me" against one shared, once-per-refresh-interval article pool, instead
# of each independently re-fetching the same general finance feeds.
#
# Deliberately general-market feeds rather than one dedicated feed per
# company: these are all large, frequently-covered companies, so their news
# reliably shows up in general finance headlines -- the per-symbol relevance
# filter (news.SYMBOL_KEYWORDS) is what actually narrows it to each stock,
# not the feed list itself.
_STOCK_POLYMARKET_CFG = {
    "keywords": ["fed ", "interest rate", "recession", "inflation"],
    "refresh_minutes": 15,
    "max_markets": 40,
    # No Polymarket markets track individual US stock prices -- same
    # always-zero treatment as SOXLUSDT.
    "relevant_symbols": [],
}
_STOCK_NEWS_CFG = {
    "feeds": [
        "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # CNBC Technology
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC Markets
        "https://www.investing.com/rss/news_25.rss",              # Investing.com stock market news
        "https://www.marketwatch.com/rss/topstories",             # MarketWatch top stories
        "https://www.benzinga.com/feed",                          # Benzinga market news
        "https://finance.yahoo.com/news/rssindex",                # Yahoo Finance
        "https://news.google.com/rss/search?q=stock+market+earnings&hl=en-US&gl=US&ceid=US:en",
    ],
    "newsapi_query": "stock market OR earnings OR Nasdaq OR S%26P 500",
    "refresh_minutes": 15,
    "lookback_hours": 12,
    "max_articles": 80,  # higher than SOXL's 60 -- one shared pool now needs to cover 10 different companies
}

# Second themed news pool, SEPARATE from SOXL_CONFIG's own news_cfg (not
# reused -- SOXL's feed/query tuning stays untouched) but built the same
# way: semiconductor feeds broadened to cover "AI infrastructure" more
# generally (data center, optical networking, servers) since several of the
# 20 symbols below (CIENUSDT/LITEUSDT/COHRUSDT = optical networking,
# SMCIUSDT/DELLUSDT/HPEUSDT/ANETUSDT = AI servers/enterprise networking)
# aren't literally chip-fab companies and would get little relevant coverage
# from a strictly semiconductor-worded feed set.
_SEMI_AI_NEWS_CFG = {
    "feeds": [
        "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # CNBC Technology
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC Markets
        "https://www.nasdaq.com/feed/rssoutbound?category=Semiconductors",  # Nasdaq Semiconductors category
        "https://www.investing.com/rss/news_25.rss",              # Investing.com stock market news
        "https://www.benzinga.com/feed",                          # Benzinga market news
        "https://finance.yahoo.com/news/rssindex",                # Yahoo Finance
        "https://techcrunch.com/feed/",                           # TechCrunch
        "https://www.zdnet.com/news/rss.xml",                     # ZDNet
        "https://news.google.com/rss/search?q=semiconductor+OR+%22AI+infrastructure%22+OR+%22data+center%22+OR+%22AI+chips%22&hl=en-US&gl=US&ceid=US:en",
    ],
    "newsapi_query": "semiconductor OR \"AI infrastructure\" OR \"data center\" OR \"AI chips\" OR networking",
    "refresh_minutes": 15,
    "lookback_hours": 12,
    "max_articles": 100,  # shared by 20 companies -- needs more headroom than the SOXL-only pool
}

# Bybit's 1x tokenized-stock USDT perpetuals (TradFi > Futures > Stocks in
# the app) -- the first 10 (AAPL/TSLA/NVDA/AMZN/GOOGL/META/MSFT/AVGO/COIN/
# MSTR) were confirmed directly against screenshots of that listing; the 28
# below were supplied directly rather than screenshotted, so they're NOT
# independently confirmed the same way (this sandboxed session still can't
# reach api.bybit.com to check itself). main()'s startup validation drops
# any symbol that turns out not to be tradable rather than crashing, as a
# backstop against a wrong/outdated ticker among these. Two names differ
# from the obvious guess that I know of: Bybit lists TSMC as TSMUSDT (not
# TSMCUSDT, used correctly below) and AMD's stock token as AMDSTOCKUSDT (not
# AMDUSDT -- not used here, but worth knowing if AMD gets added later).
#
# Split into two groups so the "테마에 대한 뉴스" (theme-relevant news) request
# gets two differently-tuned news pools rather than one generic one:
# semiconductor/AI-infrastructure names (their own vocabulary -- chips, data
# centers, export controls -- that general market feeds rarely use) vs.
# everything else (big tech/software, space/defense, consumer -- all
# large/well-covered names that show up reliably in general finance
# headlines already).
SEMI_AI_SYMBOLS = [
    "NVDAUSDT", "AVGOUSDT",  # moved here from the general pool -- both ARE semiconductor companies
    "TSMUSDT", "MUUSDT", "INTCUSDT", "AMATUSDT", "KLACUSDT", "LRCXUSDT",
    "MRVLUSDT", "ADIUSDT", "CRDOUSDT", "COHRUSDT", "LITEUSDT", "CIENUSDT",
    "ALABUSDT", "AEHRUSDT", "SMCIUSDT", "DELLUSDT", "HPEUSDT", "ANETUSDT",
]

STOCK_SYMBOLS = [
    "AAPLUSDT", "TSLAUSDT", "AMZNUSDT", "GOOGLUSDT", "METAUSDT", "MSFTUSDT",
    "COINUSDT", "MSTRUSDT", "ORCLUSDT",
    "RKLBUSDT", "RDWUSDT", "RIVNUSDT", "DKNGUSDT", "DJTUSDT",
    "COSTUSDT", "HDUSDT", "MNSTUSDT", "SONYUSDT",
]


def _make_stock_config(sym: str, news_cfg: dict) -> SymbolConfig:
    # No embedded leverage to distort the TP/SL math (unlike SOXLUSDT/the
    # removed SNXXUSDT) -- a somewhat higher range is reasonable, but kept
    # well under the original 24x/25x crypto design since this is a
    # brand-new asset class for this bot and real stocks can gap sharply on
    # earnings/news between 1h candles. Same treatment for every stock
    # symbol regardless of theme/pool.
    return SymbolConfig(
        symbol=sym,
        min_leverage=8.0, max_leverage=15.0, leverage_hard_cap=15.0,
        min_confidence_to_enter=ENTRY_THRESHOLD,
        tech_weight=0.15, volume_weight=0.15, poly_weight=0.0, news_weight=0.70,
        polymarket_cfg=_STOCK_POLYMARKET_CFG,
        news_cfg=news_cfg,
    )


SEMI_AI_CONFIGS: list[SymbolConfig] = [_make_stock_config(sym, _SEMI_AI_NEWS_CFG) for sym in SEMI_AI_SYMBOLS]
STOCK_CONFIGS: list[SymbolConfig] = [_make_stock_config(sym, _STOCK_NEWS_CFG) for sym in STOCK_SYMBOLS]

AGGRESSIVE_CONFIGS: list[SymbolConfig] = [SOXL_CONFIG] + SEMI_AI_CONFIGS + STOCK_CONFIGS

# Backward-compat alias: the strategy was originally SOXLUSDT-only, and a few
# places (dashboard's default /backtest symbol, old references) still want
# "the primary configured symbol" without iterating the whole list.
SYMBOL = SOXL_CONFIG.symbol


def state_path() -> str:
    # Own file, never shared with the main bot's data/state.json. Shared
    # across every configured symbol (trades/skip_reasons are keyed by
    # symbol inside it already).
    return os.path.join(os.getenv("DATA_DIR", "data"), "aggressive_btc_state.json")


def compute_leverage(atr_pct: float, target_pct: float = 12.0, min_leverage: float = 6.0,
                      max_leverage: float = 10.0, hard_cap: float = 10.0) -> float:
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
                          target_pct: float = 12.0, stop_loss_pct: float = 5.0) -> dict:
    """TP/SL prices implied by a target/stop expressed as % ROI on margin --
    ROI% ~= price_move_pct * leverage, so price_move_pct = ROI% / leverage.
    """
    target_move = (target_pct / 100.0) / leverage
    stop_move = (stop_loss_pct / 100.0) / leverage
    if side == "long":
        return {"tp": entry_price * (1 + target_move), "sl": entry_price * (1 - stop_move)}
    return {"tp": entry_price * (1 - target_move), "sl": entry_price * (1 + stop_move)}


def compute_signal(technical_result: dict, volume_score: float, poly_result: dict,
                    news_result: dict | None = None,
                    tech_weight: float = 0.10, volume_weight: float = 0.15,
                    poly_weight: float = 0.10, news_weight: float = 0.65,
                    entry_threshold: float = 0.4) -> dict:
    """Blends the 1h technical score (EMA trend + RSI momentum + MACD +
    Bollinger, from technical.score_timeframe) with volume confirmation,
    Polymarket sentiment (currently always 0 for both configured symbols --
    see each SymbolConfig's empty polymarket_cfg relevant_symbols), and
    general sector news into one directional call -- both long and short
    are eligible, whichever the blend favors. Polymarket and news each only
    count when they actually found something relevant (their own
    confidence), same pattern as the main bot.

    Default weights (technical 0.10, volume 0.15, polymarket 0.10, news
    0.65) are SOXLUSDT's current live tuning -- both SymbolConfig instances
    pass their own weights explicitly via AggressiveBTCStrategy.get_signal(),
    these defaults only matter for direct/test callers. Polymarket stays low
    (and inert for both symbols): its crypto-relevant markets have nothing
    to do with semiconductors or SanDisk. News is weighted heavily -- sector-
    moving events tend to show up in headlines before they fully show up in
    1h technical indicators, unlike Polymarket's weeks-to-months-out
    questions. Note that news_conf is capped at relevant_article_count/15
    (see NewsSignal.score_for_symbol), so a high news_weight is rarely fully
    realized -- with only 1-2 relevant articles (confidence ~0.07-0.13, as
    observed live for SOXLUSDT), news's actual contribution is a small
    fraction of its weight even on strongly one-sided headlines.
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

    blended = (tech_weight * tech_score + volume_weight * volume_score
               + poly_weight * (poly_score * poly_conf) + news_weight * (news_score * news_conf))
    blended = max(-1.0, min(1.0, blended))

    if blended >= entry_threshold:
        direction = "long"
    elif blended <= -entry_threshold:
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
                 poly_signal: PolymarketSignal, news_signal: NewsSignal, cfg: SymbolConfig,
                 log_dir: str = "logs"):
        self.client = client
        self.state = state
        self.notifier = notifier
        self.poly_signal = poly_signal
        self.news_signal = news_signal
        self.cfg = cfg
        self.symbol = cfg.symbol
        self.log_dir = log_dir
        self._last_signal_log_ts = 0.0

    # -- signal ---------------------------------------------------------
    def get_signal(self) -> dict:
        candles = self.client.get_klines(self.symbol, TIMEFRAME, limit=200)
        tech_result = technical.score_timeframe(candles, self.cfg.tech_cfg)
        vol_score = technical.volume_score(candles)
        poly_result = self.poly_signal.score(self.symbol)
        news_result = self.news_signal.score_for_symbol(self.symbol)
        return compute_signal(tech_result, vol_score, poly_result, news_result,
                               tech_weight=self.cfg.tech_weight, volume_weight=self.cfg.volume_weight,
                               poly_weight=self.cfg.poly_weight, news_weight=self.cfg.news_weight,
                               entry_threshold=self.cfg.min_confidence_to_enter)

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

        This is observability only -- it must never be able to block the
        actual trading decision right after it (set_skip_reason / try_enter's
        entry path). Any failure here is caught and logged, not re-raised.
        """
        now = time.time()
        if now - self._last_signal_log_ts < SIGNAL_LOG_INTERVAL_SEC:
            return
        self._last_signal_log_ts = now
        try:
            log_decision(self.log_dir, {
                "event": "signal_check", "symbol": self.symbol, "direction": signal.get("direction"),
                "score": signal.get("score"), "signal": self._signal_for_log(signal),
            })
        except Exception:
            logger.exception("aggressive_btc: failed to log signal_check decision")

    def try_enter(self):
        if self.state.get_trade(self.symbol) is not None:
            return
        # Global cap: only one of the 11 configured symbols may be in a
        # position at a time (see module docstring) -- this symbol has no
        # open trade (checked above), so open_trade_count() > 0 here means
        # some OTHER symbol currently does.
        if self.state.open_trade_count() > 0:
            self.state.set_skip_reason(self.symbol, "다른 종목이 이미 포지션 보유 중 (동시 1개 제한)")
            return

        signal = self.get_signal()
        # Unthrottled, unlike _log_signal_check's decisions.jsonl record below --
        # this is the only proof-of-life trace for a "still neutral" tick
        # anywhere in Application logs (set_skip_reason below writes to the
        # state file but never calls logger.*), which made a genuinely stuck
        # loop indistinguishable from a quiet-but-alive one from the logs alone.
        logger.info("aggressive_btc: %s tick direction=%s score=%+.2f atr=%.4f close=%.4f",
                    self.symbol, signal.get("direction"), signal.get("score", 0.0),
                    signal.get("atr", 0.0), signal.get("close", 0.0))
        self._log_signal_check(signal)
        if signal["atr"] <= 0 or signal["close"] <= 0:
            self.state.set_skip_reason(self.symbol, "가격/ATR 데이터 없음")
            return
        if signal["direction"] == "neutral":
            self.state.set_skip_reason(
                self.symbol, f"관망 중 - 신호 약함 ({self._format_signal_breakdown(signal)}, "
                             f"기준 ±{self.cfg.min_confidence_to_enter:.2f})")
            return

        equity = self.client.get_equity_usdt()
        margin = equity * (self.cfg.margin_pct_of_equity / 100.0)
        available = self.client.get_available_balance_usdt()
        if available is not None:
            margin = min(margin, available)
        if margin <= 0:
            self.state.set_skip_reason(self.symbol, "여유 증거금 없음")
            return

        atr_pct = signal["atr"] / signal["close"] * 100.0
        leverage = compute_leverage(atr_pct, target_pct=self.cfg.target_pct,
                                     min_leverage=self.cfg.min_leverage, max_leverage=self.cfg.max_leverage,
                                     hard_cap=self.cfg.leverage_hard_cap)

        inst = self.client.get_instrument_info(self.symbol)
        leverage = min(leverage, inst.max_leverage)
        # BybitClient.set_leverage truncates to a whole number when it talks to
        # the exchange -- round down here too, BEFORE computing TP/SL prices,
        # so the price targets are derived from the SAME leverage that actually
        # ends up applied to the position. Otherwise a fractional leverage here
        # (e.g. 21.6x) vs an integer 21x on the exchange silently shifts the
        # realized ROI% at TP/SL away from the intended target.
        leverage = float(int(leverage))

        side = signal["direction"]
        entry_price = self.client.get_last_price(self.symbol)
        prices = compute_tp_sl_prices(entry_price, side, leverage,
                                       target_pct=self.cfg.target_pct, stop_loss_pct=self.cfg.stop_loss_pct)

        sizing = compute_qty_fixed_margin(
            margin=margin, leverage=leverage, entry_price=entry_price,
            qty_step=inst.qty_step, min_qty=inst.min_qty, min_notional=MIN_ORDER_NOTIONAL_USDT,
        )
        if not sizing.ok:
            # margin_pct_of_equity of equity alone doesn't clear the exchange's
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
                logger.info("aggressive_btc: %s skip entry, %s", self.symbol, sizing.reason)
                self.state.set_skip_reason(self.symbol, f"주문 크기 조건 미충족 ({sizing.reason})")
                return

        qty = self.client.round_qty(self.symbol, sizing.qty)
        sl_price = self.client.round_price(self.symbol, prices["sl"])
        tp_price = self.client.round_price(self.symbol, prices["tp"])

        try:
            self.client.set_leverage(self.symbol, leverage)
            self.client.open_position(self.symbol, side, qty, stop_loss=sl_price, take_profit=tp_price)
        except BybitAPIError as exc:
            logger.error("aggressive_btc: failed to open position for %s: %s", self.symbol, exc)
            self.state.set_skip_reason(self.symbol, f"주문 실패 ({exc})")
            return

        # Same safety-first pattern as the main bot: verify SL/TP actually
        # attached (Bybit can silently drop them even when the base order
        # fills) -- close immediately rather than ever hold an unprotected
        # leveraged position.
        exchange_position = self.client.get_position(self.symbol)
        attached = bool(exchange_position) and exchange_position["stop_loss"] > 0 and exchange_position["take_profit"] > 0
        if not attached:
            logger.critical("aggressive_btc: %s opened with no protective SL/TP -- closing immediately", self.symbol)
            try:
                self.client.close_position(self.symbol, side, qty)
            except BybitAPIError as exc:
                logger.critical("aggressive_btc: COULD NOT CLOSE UNPROTECTED POSITION -- MANUAL ACTION REQUIRED: %s", exc)
            self.notifier.send(f"[긴급] aggressive_btc: {self.symbol} SL/TP 설정 실패 - 긴급 청산 시도. Bybit 앱에서 직접 확인하세요.")
            self.state.set_skip_reason(self.symbol, "SL/TP 설정 실패로 긴급 청산됨")
            return

        trade = {
            "symbol": self.symbol, "side": side, "entry_price": entry_price, "qty": qty,
            "margin": margin, "leverage": leverage, "current_sl": sl_price, "current_tp": tp_price,
            "opened_at": time.time(), "last_hour_checked": 0,
        }
        self.state.set_trade(self.symbol, trade)
        self.state.clear_skip_reason(self.symbol)
        msg = (f"[aggressive_btc 진입] {self.symbol} {side.upper()} qty={qty} entry~{entry_price:.2f} "
               f"SL={sl_price:.2f} TP={tp_price:.2f} lev={leverage:.1f}x score={signal['score']:+.2f}")
        logger.info(msg)
        self.notifier.send(msg)
        try:
            log_decision(self.log_dir, {"event": "entry", "symbol": self.symbol, "side": side, "qty": qty,
                                         "entry_price": entry_price, "sl": sl_price, "tp": tp_price,
                                         "leverage": leverage, "signal": self._signal_for_log(signal)})
        except Exception:
            logger.exception("aggressive_btc: failed to log entry decision")

    # -- management ---------------------------------------------------------
    def manage_open_position(self):
        trade = self.state.get_trade(self.symbol)
        if trade is None:
            return

        exchange_position = self.client.get_position(self.symbol)
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
            logger.info("aggressive_btc: %s hourly cutoff at hour %d, ROI=%.2f%% still negative -- closing",
                        self.symbol, new_mark, current_roi)
            self._close_and_settle(trade, "hourly_cutoff")
            return

        self.state.set_trade(self.symbol, trade)

    def _close_and_settle(self, trade: dict, reason: str, already_closed: bool = False):
        if not already_closed:
            try:
                close_qty = self.client.round_qty(self.symbol, trade["qty"])
                self.client.close_position(self.symbol, trade["side"], close_qty)
            except BybitAPIError as exc:
                logger.error("aggressive_btc: failed to close %s: %s", self.symbol, exc)
                return

        closed = self.client.get_closed_pnl(self.symbol)
        if closed and closed["updated_time_ms"] / 1000.0 >= trade["opened_at"]:
            pnl = closed["closed_pnl"]
            pnl_is_estimate = False
        else:
            try:
                exit_price = self.client.get_last_price(self.symbol)
            except BybitAPIError:
                exit_price = trade["entry_price"]
            if trade["side"] == "long":
                pnl = (exit_price - trade["entry_price"]) * trade["qty"]
            else:
                pnl = (trade["entry_price"] - exit_price) * trade["qty"]
            pnl_is_estimate = True

        self.state.set_trade(self.symbol, None)
        self.state.record_closed_trade({
            "symbol": self.symbol, "side": trade["side"], "entry_price": trade["entry_price"],
            "qty": trade["qty"], "pnl": pnl, "reason": reason, "pnl_is_estimate": pnl_is_estimate,
            "leverage": trade["leverage"], "margin": trade["margin"], "closed_at": time.time(),
        })
        est_tag = " (est.)" if pnl_is_estimate else ""
        msg = f"[aggressive_btc 종료:{reason}] {self.symbol} {trade['side'].upper()} PnL={pnl:+.4f} USDT{est_tag}"
        logger.info(msg)
        self.notifier.send(msg)
        try:
            log_decision(self.log_dir, {"event": "exit", "symbol": self.symbol, "side": trade["side"],
                                         "reason": reason, "pnl": pnl, "pnl_is_estimate": pnl_is_estimate})
        except Exception:
            logger.exception("aggressive_btc: failed to log exit decision")

    # -- loop ---------------------------------------------------------
    def tick(self):
        if self.state.get_trade(self.symbol) is not None:
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
    symbols_desc = ", ".join(
        f"{sc.symbol}(target=+{sc.target_pct:.0f}% SL=-{sc.stop_loss_pct:.0f}% "
        f"lev=[{sc.min_leverage:.0f}-{sc.max_leverage:.0f}]x cap={sc.leverage_hard_cap:.0f}x)"
        for sc in AGGRESSIVE_CONFIGS
    )
    logger.warning("aggressive_btc starting | mode=%s | symbols: %s", mode, symbols_desc)

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
    # Write the state file immediately, before anything else below has a
    # chance to fail -- the dashboard's "Aggressive" cards only render when
    # this file exists (see dashboard/app.py's _build_aggressive_btc_view),
    # so this guarantees they show up as soon as this process starts,
    # independent of whether try_enter()/get_signal() ever succeed afterward.
    state.save()
    log_dir = os.getenv("LOG_DIR", "logs")  # same shared decisions.jsonl the main bot and dashboard use

    # Many of the configured stock tickers aren't independently verified
    # against Bybit's live API (see module docstring) -- check each one
    # exists/is tradable before building a strategy for it, so a wrong or
    # delisted ticker just gets skipped (logged + notified) instead of
    # taking down every other configured symbol at startup.
    valid_configs = []
    invalid_symbols = []
    for symbol_cfg in AGGRESSIVE_CONFIGS:
        try:
            client.get_instrument_info(symbol_cfg.symbol)
        except BybitAPIError as exc:
            invalid_symbols.append(symbol_cfg.symbol)
            logger.warning("aggressive_btc: %s not tradable on this exchange, skipping: %s",
                            symbol_cfg.symbol, exc)
            continue
        valid_configs.append(symbol_cfg)
    if invalid_symbols:
        notifier.send(f"[aggressive_btc] 아래 심볼은 거래소에서 확인되지 않아 제외됨: {', '.join(invalid_symbols)}")

    # Signal instances are shared across configs whose cfg dict is the SAME
    # object (e.g. all STOCK_CONFIGS point at the same _STOCK_NEWS_CFG, all
    # SEMI_AI_CONFIGS at the same _SEMI_AI_NEWS_CFG) -- keyed by id() so each
    # pool's symbols fetch their shared RSS/Polymarket pool only once per
    # refresh interval, not once each.
    poly_cache: dict[int, PolymarketSignal] = {}
    news_cache: dict[int, NewsSignal] = {}
    strategies = []
    for symbol_cfg in valid_configs:
        poly_key = id(symbol_cfg.polymarket_cfg)
        if poly_key not in poly_cache:
            poly_cache[poly_key] = PolymarketSignal(symbol_cfg.polymarket_cfg)
        news_key = id(symbol_cfg.news_cfg)
        if news_key not in news_cache:
            news_cache[news_key] = NewsSignal(symbol_cfg.news_cfg, secrets.newsapi_key)
        strategies.append(AggressiveBTCStrategy(client, state, notifier, poly_cache[poly_key], news_cache[news_key],
                                                 symbol_cfg, log_dir))

    active_symbols_desc = ", ".join(sc.symbol for sc in valid_configs)
    notifier.send(f"aggressive_btc 시작됨 - {mode} - {active_symbols_desc}")
    # Guarantees at least one row in "최근 판단 로그" the moment this process
    # starts, regardless of whether the tick loop below ever gets anywhere --
    # previously the very first row could only ever be an entry/exit/
    # signal_check, all of which require try_enter() to run cleanly first.
    try:
        log_decision(log_dir, {"event": "started", "symbol": "+".join(sc.symbol for sc in valid_configs),
                                "mode": mode})
    except Exception:
        logger.exception("aggressive_btc: failed to log started decision")

    while True:
        loop_start = time.time()
        for strategy in strategies:
            try:
                strategy.tick()
            except Exception:
                logger.exception("aggressive_btc: unhandled error in main loop tick for %s", strategy.symbol)
        elapsed = time.time() - loop_start
        time.sleep(max(1.0, POLL_SEC - elapsed))


if __name__ == "__main__":
    main()
