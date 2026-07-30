"""Technical-analysis signal: multi-timeframe trend/momentum/volatility scoring.

Every score is normalized to roughly [-1, 1] where positive = bullish (favors
long), negative = bearish (favors short). Nothing here places orders; it only
turns OHLCV candles into numbers strategy.py can weigh against news/polymarket.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def to_dataframe(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, x)))


def score_timeframe(candles: list[dict], cfg: dict) -> dict:
    """Returns {score, atr, close, rsi} for a single timeframe's candles."""
    df = to_dataframe(candles)
    if len(df) < max(cfg.get("ema_slow", 26), cfg.get("rsi_period", 14)) + 5:
        return {"score": 0.0, "atr": 0.0, "close": float(df["close"].iloc[-1]) if len(df) else 0.0, "rsi": 50.0}

    close = df["close"]
    fast_ema = ema(close, cfg.get("ema_fast", 12))
    slow_ema = ema(close, cfg.get("ema_slow", 26))
    rsi_series = rsi(close, cfg.get("rsi_period", 14))
    _, _, hist = macd(close, cfg.get("ema_fast", 12), cfg.get("ema_slow", 26))
    upper, mid, lower = bollinger(close, 20, 2.0)
    atr_series = atr(df, cfg.get("atr_period", 14))

    last_close = float(close.iloc[-1])
    last_atr = float(atr_series.iloc[-1]) if not np.isnan(atr_series.iloc[-1]) else 0.0
    last_atr = max(last_atr, last_close * 0.0005)  # floor so we never divide by ~0

    trend_score = _clip((float(fast_ema.iloc[-1]) - float(slow_ema.iloc[-1])) / last_atr)
    momentum_score = _clip((float(rsi_series.iloc[-1]) - 50.0) / 35.0)
    macd_score = _clip(float(hist.iloc[-1]) / last_atr)
    band_width = float(upper.iloc[-1] - lower.iloc[-1]) or last_atr * 4
    boll_score = _clip((last_close - float(mid.iloc[-1])) / (band_width / 2 or 1))

    score = _clip(0.35 * trend_score + 0.25 * momentum_score + 0.25 * macd_score + 0.15 * boll_score)

    return {
        "score": score,
        "atr": last_atr,
        "close": last_close,
        "rsi": float(rsi_series.iloc[-1]),
    }


def range_levels(candles: list[dict], lookback: int = 20) -> dict:
    """Rolling high/low over the last `lookback` candles, used to spot a trading
    range for mean-reversion entries when the trend signal is neutral.
    """
    df = to_dataframe(candles)
    if len(df) < lookback:
        return {"range_high": 0.0, "range_low": 0.0}
    recent = df.tail(lookback)
    return {"range_high": float(recent["high"].max()), "range_low": float(recent["low"].min())}


def volume_score(candles: list[dict], lookback: int = 20, direction_lookback: int = 3) -> float:
    """Relative volume + whether recent price action is confirming the direction.

    Direction is measured over the last `direction_lookback` candles, not just
    the most recent one -- a single candle (e.g. a brief pullback wick within
    an ongoing uptrend, often exactly where a volume spike shows up) shouldn't
    be able to swing this component hard against the actual multi-candle
    direction. Observed live: a lone red 15m candle with above-average volume
    scored strongly bearish mid-uptrend, reinforcing a wrong short call.
    """
    df = to_dataframe(candles)
    if len(df) < lookback + 2:
        return 0.0
    recent = df.tail(lookback)
    avg_vol = recent["volume"].iloc[:-1].mean() or 1.0
    last_vol = recent["volume"].iloc[-1]
    rel_vol = _clip((last_vol / avg_vol - 1.0), -1.0, 2.0)  # can exceed 1 on spikes

    span = max(1, min(direction_lookback, len(recent) - 1))
    price_change = recent["close"].iloc[-1] - recent["close"].iloc[-1 - span]
    direction = 1.0 if price_change > 0 else (-1.0 if price_change < 0 else 0.0)

    # volume alone isn't directional -- it amplifies whatever direction price just moved.
    return _clip(direction * min(abs(rel_vol), 1.5) * 0.66)


def multi_timeframe_score(klines_by_tf: dict[str, list[dict]], timeframes: list[str], cfg: dict) -> dict:
    """Combines per-timeframe scores, weighting longer timeframes more (trend filter).

    The step between consecutive timeframes' weight is `timeframe_weight_step`
    (default 1.0, i.e. weights 1, 2, 3 for 3 timeframes -- the original
    behavior). A slow-moving higher timeframe (e.g. EMA12/26 on 4h candles is
    a 2-4 day lookback) lags a genuine fresh trend reversal by design, so
    weighting it 3x a fast one can keep the blended score reading the *old*
    direction well after price has already turned. Lowering the step flattens
    that gap so a newer shift on faster timeframes isn't drowned out as hard.

    Returns {score, atr (from the shortest/execution timeframe), per_tf: {...}}.
    """
    per_tf = {}
    weighted_sum = 0.0
    weight_total = 0.0
    weight_step = cfg.get("timeframe_weight_step", 1.0)
    for i, tf in enumerate(timeframes):
        candles = klines_by_tf.get(tf, [])
        if not candles:
            continue
        result = score_timeframe(candles, cfg)
        per_tf[tf] = result
        weight = 1.0 + i * weight_step  # later (longer) timeframes weigh more
        weighted_sum += result["score"] * weight
        weight_total += weight

    combined = weighted_sum / weight_total if weight_total else 0.0
    exec_tf = timeframes[0] if timeframes else None
    exec_atr = per_tf.get(exec_tf, {}).get("atr", 0.0) if exec_tf else 0.0
    exec_close = per_tf.get(exec_tf, {}).get("close", 0.0) if exec_tf else 0.0

    vol_score = volume_score(klines_by_tf.get(exec_tf, []),
                              direction_lookback=cfg.get("volume_direction_lookback", 3)) if exec_tf else 0.0
    range_info = range_levels(klines_by_tf.get(exec_tf, []), cfg.get("range_lookback", 20)) if exec_tf else \
        {"range_high": 0.0, "range_low": 0.0}

    return {
        "score": _clip(combined),
        "atr": exec_atr,
        "close": exec_close,
        "volume_score": vol_score,
        "range_high": range_info["range_high"],
        "range_low": range_info["range_low"],
        "per_tf": per_tf,
    }
