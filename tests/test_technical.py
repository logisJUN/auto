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
