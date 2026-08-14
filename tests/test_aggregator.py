from bot.signals import aggregator

WEIGHTS = {"technical": 0.45, "volume": 0.15, "news": 0.20, "polymarket": 0.20}


def test_all_signals_bullish_gives_strong_long_confidence():
    technical = {"score": 0.8, "atr": 1.0, "close": 100.0, "volume_score": 0.5}
    news = {"score": 0.6, "confidence": 0.8, "sample": []}
    poly = {"score": 0.5, "confidence": 0.6, "sample": []}
    result = aggregator.aggregate(technical, news, poly, WEIGHTS)
    assert result["direction"] == "long"
    assert result["score"] > 0.3
    assert result["confidence"] > 0.5


def test_conflicting_signals_lower_confidence_than_agreement():
    technical = {"score": 0.8, "atr": 1.0, "close": 100.0, "volume_score": 0.5}
    news_agree = {"score": 0.6, "confidence": 0.8, "sample": []}
    news_conflict = {"score": -0.6, "confidence": 0.8, "sample": []}
    poly = {"score": 0.0, "confidence": 0.0, "sample": []}

    agree = aggregator.aggregate(technical, news_agree, poly, WEIGHTS)
    conflict = aggregator.aggregate(technical, news_conflict, poly, WEIGHTS)
    assert agree["confidence"] > conflict["confidence"]


def test_no_data_signals_default_to_neutral():
    technical = {"score": 0.0, "atr": 1.0, "close": 100.0, "volume_score": 0.0}
    news = {"score": 0.0, "confidence": 0.0, "sample": []}
    poly = {"score": 0.0, "confidence": 0.0, "sample": []}
    result = aggregator.aggregate(technical, news, poly, WEIGHTS)
    assert result["direction"] == "neutral"


def test_low_confidence_news_has_less_influence_than_high_confidence():
    technical = {"score": 0.0, "atr": 1.0, "close": 100.0, "volume_score": 0.0}
    poly = {"score": 0.0, "confidence": 0.0, "sample": []}
    low_conf = aggregator.aggregate(technical, {"score": -0.9, "confidence": 0.1, "sample": []}, poly, WEIGHTS)
    high_conf = aggregator.aggregate(technical, {"score": -0.9, "confidence": 0.9, "sample": []}, poly, WEIGHTS)
    assert abs(high_conf["score"]) > abs(low_conf["score"])


def test_missing_funding_arg_behaves_like_before_funding_existed():
    technical = {"score": 0.8, "atr": 1.0, "close": 100.0, "volume_score": 0.5}
    news = {"score": 0.6, "confidence": 0.8, "sample": []}
    poly = {"score": 0.5, "confidence": 0.6, "sample": []}
    result = aggregator.aggregate(technical, news, poly, WEIGHTS)
    assert result["funding_rate"] is None
    assert result["components"]["funding"]["weight"] == 0.0


def test_recent_extension_atr_mult_passes_through_from_technical():
    technical = {"score": 0.8, "atr": 1.0, "close": 100.0, "volume_score": 0.5,
                 "recent_extension_atr_mult": 4.2}
    news = {"score": 0.0, "confidence": 0.0, "sample": []}
    poly = {"score": 0.0, "confidence": 0.0, "sample": []}
    result = aggregator.aggregate(technical, news, poly, WEIGHTS)
    assert result["recent_extension_atr_mult"] == 4.2


def test_recent_extension_atr_mult_defaults_to_zero_when_missing():
    technical = {"score": 0.8, "atr": 1.0, "close": 100.0, "volume_score": 0.5}
    news = {"score": 0.0, "confidence": 0.0, "sample": []}
    poly = {"score": 0.0, "confidence": 0.0, "sample": []}
    result = aggregator.aggregate(technical, news, poly, WEIGHTS)
    assert result["recent_extension_atr_mult"] == 0.0


def test_extreme_contrarian_funding_pulls_score_toward_its_own_direction():
    technical = {"score": 0.7, "atr": 1.0, "close": 100.0, "volume_score": 0.0}
    news = {"score": 0.0, "confidence": 0.0, "sample": []}
    poly = {"score": 0.0, "confidence": 0.0, "sample": []}
    weights_with_funding = {**WEIGHTS, "funding": 0.5}

    no_funding = aggregator.aggregate(technical, news, poly, weights_with_funding,
                                       funding={"score": 0.0, "confidence": 0.0})
    bearish_funding = aggregator.aggregate(technical, news, poly, weights_with_funding,
                                            funding={"score": -1.0, "confidence": 1.0, "funding_rate": 0.0005})
    assert bearish_funding["score"] < no_funding["score"]
    assert bearish_funding["funding_rate"] == 0.0005
