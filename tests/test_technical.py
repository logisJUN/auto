import pytest

from bot.signals import technical

TECH_CFG = {"ema_fast": 12, "ema_slow": 26, "rsi_period": 14, "atr_period": 14}


def _make_candles(prices, base_ts=1_700_000_000, step_sec=900):
    candles = []
    for i, p in enumerate(prices):
        candles.append({
            "ts": base_ts + i * step_sec * 1000,
            "open": p, "high": p * 1.001, "low": p * 0.999, "close": p,
            "volume": 100 + i,
        })
    return candles


def test_uptrend_scores_positive():
    prices = [100 + i * 0.8 for i in range(80)]
    candles = _make_candles(prices)
    result = technical.score_timeframe(candles, TECH_CFG)
    assert result["score"] > 0


def test_downtrend_scores_negative():
    prices = [200 - i * 0.8 for i in range(80)]
    candles = _make_candles(prices)
    result = technical.score_timeframe(candles, TECH_CFG)
    assert result["score"] < 0


def test_flat_market_scores_near_zero():
    prices = [100.0 for _ in range(80)]
    candles = _make_candles(prices)
    result = technical.score_timeframe(candles, TECH_CFG)
    assert abs(result["score"]) < 0.2


def test_insufficient_data_returns_neutral_without_crashing():
    candles = _make_candles([100, 101, 102])
    result = technical.score_timeframe(candles, TECH_CFG)
    assert result["score"] == 0.0


def test_volume_score_amplifies_direction_on_spike():
    prices = [100 + i * 0.1 for i in range(25)]
    candles = _make_candles(prices)
    candles[-1]["volume"] = 1000  # spike on the up-move
    score = technical.volume_score(candles)
    assert score > 0


def test_volume_score_not_flipped_by_a_single_pullback_candle():
    """A brief red candle (with a volume spike) inside an ongoing uptrend
    shouldn't swing this component bearish by itself -- direction is judged
    over the last few candles, not just the very last one.
    """
    prices = [100 + i * 0.5 for i in range(25)]
    candles = _make_candles(prices)
    # last candle dips from the prior close, but is still above the close
    # from 3 candles back -- net direction over that span is still up.
    candles[-1]["close"] = candles[-2]["close"] - 0.2
    candles[-1]["volume"] = 1000  # spike on the down-tick

    score = technical.volume_score(candles, direction_lookback=3)

    assert score > 0


def test_volume_score_direction_lookback_of_one_matches_old_single_candle_behavior():
    prices = [100 + i * 0.5 for i in range(25)]
    candles = _make_candles(prices)
    candles[-1]["close"] = candles[-2]["close"] - 0.2
    candles[-1]["volume"] = 1000

    score = technical.volume_score(candles, direction_lookback=1)

    assert score < 0


def test_timeframe_weight_step_reduces_slower_timeframe_dominance():
    up_candles = _make_candles([100 + i * 0.8 for i in range(80)])    # bullish
    down_candles = _make_candles([200 - i * 0.8 for i in range(80)])  # bearish
    klines = {"15": up_candles, "240": down_candles}

    result_default = technical.multi_timeframe_score(klines, ["15", "240"], TECH_CFG)
    result_flatter = technical.multi_timeframe_score(
        klines, ["15", "240"], {**TECH_CFG, "timeframe_weight_step": 0.5})

    # both lean toward the slower/bearish timeframe's reading, but the
    # flattened weighting lets the faster/bullish timeframe count for
    # relatively more, pulling the combined score less negative.
    assert result_flatter["score"] > result_default["score"]


def test_recent_extension_positive_after_a_sharp_up_move():
    prices = [100.0] * 20 + [100, 101, 103, 106, 110, 115, 120]  # sharp climb at the end
    candles = _make_candles(prices)
    # atr=1.0 for simplicity -> a 20-point move over the last 6 candles is 20x ATR
    ext = technical.recent_extension(candles, atr=1.0, lookback=6)
    assert ext > 0
    assert ext == pytest.approx((120 - 100) / 1.0)


def test_recent_extension_negative_after_a_sharp_down_move():
    prices = [100.0] * 20 + [100, 99, 97, 94, 90, 85, 80]  # sharp drop at the end
    candles = _make_candles(prices)
    ext = technical.recent_extension(candles, atr=1.0, lookback=6)
    assert ext < 0


def test_recent_extension_zero_on_insufficient_data_or_no_atr():
    candles = _make_candles([100, 101, 102])
    assert technical.recent_extension(candles, atr=1.0, lookback=6) == 0.0
    candles_enough = _make_candles([100 + i for i in range(10)])
    assert technical.recent_extension(candles_enough, atr=0.0, lookback=6) == 0.0


def test_multi_timeframe_score_surfaces_recent_extension():
    prices = [100.0] * 40 + [100, 101, 103, 106, 110, 115, 120]
    candles = _make_candles(prices)
    result = technical.multi_timeframe_score({"15": candles}, ["15"], TECH_CFG)
    assert result["recent_extension_atr_mult"] > 0


def test_range_levels_picks_high_low_over_lookback():
    prices = [100, 105, 95, 102, 98, 101, 99, 100]
    candles = _make_candles(prices)
    result = technical.range_levels(candles, lookback=len(prices))
    assert result["range_high"] == max(p * 1.001 for p in prices)
    assert result["range_low"] == min(p * 0.999 for p in prices)


def test_range_levels_insufficient_data_returns_zero():
    candles = _make_candles([100, 101])
    result = technical.range_levels(candles, lookback=20)
    assert result["range_high"] == 0.0
    assert result["range_low"] == 0.0
