"""Backtests aggressive_btc's TECHNICAL + VOLUME signal against real
historical candles. Does NOT (cannot) backtest the news component.

Why news can't be backtested: there's no historical archive of the exact RSS
feeds/NewsAPI results aggressive_btc would have seen at each past point in
time -- only today's current headlines are available.

IMPORTANT -- this does NOT reuse live aggressive_btc.compute_signal()'s
0.10/0.15/0.10/0.65 (technical/volume/polymarket/news) weights. With news
forced to zero, that blend can reach at most 0.10*1.0 + 0.15*1.0 = 0.25 --
it can NEVER cross the live 0.40 entry threshold, so a backtest using the
live weights would always produce exactly zero trades and tell us nothing.
Instead, this uses its OWN explicit technical/volume-only weights and
threshold (defaults below: 0.75/0.15, threshold 0.5 -- the ORIGINAL
pre-news weighting this strategy launched with), to answer a well-defined,
different question: "would the technical+volume formula alone, at a scale
where it can actually trigger trades, have been profitable?" -- not "would
the current live (news-dominated) strategy have been profitable," which
is simply not answerable without historical news data.

Known simplifications (bias to be aware of when reading results):
  - If a single candle's high/low range touches BOTH the TP and SL price,
    this assumes the WORSE outcome (SL) happened first -- a candle doesn't
    tell you the intra-candle order, and assuming the better outcome would
    make results look better than they'd likely be live.
  - Entry/exit prices are NOT rounded to the exchange's actual tick size
    (unlike live trading) -- a small, usually negligible approximation.
  - No slippage modeled -- fills are assumed at exactly the TP/SL/cutoff
    price, which real market/trigger orders won't always get exactly.

Fees ARE modeled (round_trip_fee_pct_of_notional below, default 0.11% --
the main bot's observed round-trip taker fee, see config.yaml). This matters
a lot more than it sounds: fee cost in ROI% terms scales with LEVERAGE (fee
is charged on notional = margin * leverage, but ROI is measured against
margin), so at ~10x leverage a single round trip costs roughly 10 * 0.11% =
~1.1% of margin -- a strategy that trades often can lose most or all of an
otherwise-positive gross return to fees alone. Watch trade_count: a low-
frequency combo (10-15 trades over 90 days) is far more fee-resistant than
a high-frequency one (300+), even if the high-frequency one's GROSS number
looks better.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.aggressive_btc import (
    TECH_CFG,
    compute_leverage,
    compute_tp_sl_prices,
    should_hourly_cutoff,
)
from bot.exchange.bybit_client import BybitClient
from bot.signals import technical


def fetch_historical_klines(client: BybitClient, symbol: str, interval: str,
                             total_candles: int, page_size: int = 1000) -> list[dict]:
    """Walks backward in time via Bybit's `end` kline param to assemble more
    history than a single call returns. Returns oldest->newest, deduplicated.
    Stops early if Bybit runs out of history to return (a newly-listed
    symbol, or reaching the exchange's retention limit).
    """
    collected: list[dict] = []
    end_ms: int | None = None
    seen_ts: set[int] = set()
    while len(collected) < total_candles:
        batch = client.get_klines(symbol, interval, limit=page_size, end_time_ms=end_ms)
        batch = [c for c in batch if c["ts"] not in seen_ts]
        if not batch:
            break
        collected = batch + collected
        seen_ts.update(c["ts"] for c in batch)
        end_ms = batch[0]["ts"] - 1
        if len(batch) < page_size:
            break  # exhausted whatever history Bybit has
    return collected[-total_candles:] if len(collected) > total_candles else collected


@dataclass
class BacktestTrade:
    side: str
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    leverage: float
    roi_pct: float       # NET of the round-trip fee below
    fee_pct: float        # fee cost alone, in ROI% terms (= round_trip_fee_pct_of_notional * leverage)
    reason: str  # "tp" / "sl" / "hourly_cutoff" / "end_of_data"


def simulate(candles: list[dict], warmup: int = 60, window: int = 200,
             tech_weight: float = 0.75, volume_weight: float = 0.15,
             entry_threshold: float = 0.5,
             round_trip_fee_pct_of_notional: float = 0.11) -> dict:
    """Walks `candles` (oldest->newest 1h klines) one at a time, replaying a
    technical+volume-only signal and aggressive_btc's real entry/TP/SL/
    hourly-cutoff rules. See module docstring for why this uses its own
    tech_weight/volume_weight/entry_threshold instead of live
    compute_signal()'s weights (which, with news forced to 0, could never
    trigger a trade at all).

    `warmup` candles are skipped at the start so score_timeframe() always
    has enough history (EMA/RSI/ATR need a run-up) to return a real score
    instead of the "not enough data" neutral default. `window` mirrors
    get_signal()'s live limit=200 kline fetch -- the score at candle i is
    computed from the same-sized trailing window a live tick would have seen.
    """
    trades: list[BacktestTrade] = []
    i = warmup
    while i < len(candles):
        w = candles[max(0, i - window + 1): i + 1]
        tech_result = technical.score_timeframe(w, TECH_CFG)
        vol_score = technical.volume_score(w)
        tech_score = tech_result.get("score", 0.0)
        atr = tech_result.get("atr", 0.0)
        close = tech_result.get("close", 0.0)

        blended = max(-1.0, min(1.0, tech_weight * tech_score + volume_weight * vol_score))
        if blended >= entry_threshold:
            side = "long"
        elif blended <= -entry_threshold:
            side = "short"
        else:
            side = None

        if side is None or atr <= 0 or close <= 0:
            i += 1
            continue

        atr_pct = atr / close * 100.0
        leverage = float(int(compute_leverage(atr_pct)))
        entry_price = candles[i]["close"]
        prices = compute_tp_sl_prices(entry_price, side, leverage)

        exit_idx, exit_price, reason = None, None, "end_of_data"
        last_hour_checked = 0
        for j in range(i + 1, len(candles)):
            c = candles[j]
            hit_tp = c["high"] >= prices["tp"] if side == "long" else c["low"] <= prices["tp"]
            hit_sl = c["low"] <= prices["sl"] if side == "long" else c["high"] >= prices["sl"]
            if hit_sl:  # conservative: SL wins any same-candle ambiguity (see module docstring)
                exit_idx, exit_price, reason = j, prices["sl"], "sl"
                break
            if hit_tp:
                exit_idx, exit_price, reason = j, prices["tp"], "tp"
                break

            hours_open = j - i
            unrealized_roi = (((c["close"] - entry_price) / entry_price) * 100.0 * leverage) if side == "long" \
                else (((entry_price - c["close"]) / entry_price) * 100.0 * leverage)
            should_close, last_hour_checked = should_hourly_cutoff(hours_open, unrealized_roi, last_hour_checked)
            if should_close:
                exit_idx, exit_price, reason = j, c["close"], "hourly_cutoff"
                break

        if exit_idx is None:
            break  # ran out of candles mid-trade -- stop rather than fabricate an exit

        price_move_pct = (((exit_price - entry_price) / entry_price) * 100.0) if side == "long" \
            else (((entry_price - exit_price) / entry_price) * 100.0)
        gross_roi = price_move_pct * leverage
        # Fee is charged on notional (margin * leverage), but ROI is measured
        # against margin -- so its cost in ROI% terms scales with leverage.
        fee_pct = round_trip_fee_pct_of_notional * leverage
        net_roi = gross_roi - fee_pct
        trades.append(BacktestTrade(
            side=side, entry_ts=candles[i]["ts"], entry_price=entry_price,
            exit_ts=candles[exit_idx]["ts"], exit_price=exit_price,
            leverage=leverage, roi_pct=net_roi, fee_pct=fee_pct, reason=reason,
        ))
        i = exit_idx + 1

    wins = [t for t in trades if t.roi_pct > 0]
    total_roi = sum(t.roi_pct for t in trades)
    total_fees = sum(t.fee_pct for t in trades)
    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1

    return {
        "trades": trades,
        "trade_count": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "total_roi_pct": total_roi,              # net of fees
        "avg_roi_pct": (total_roi / len(trades)) if trades else None,  # net of fees
        "total_fee_pct": total_fees,
        "by_reason": by_reason,
    }
