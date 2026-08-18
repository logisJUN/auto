from bot.signals import funding

CFG = {"normalize_pct": 0.05}


def test_missing_rate_is_neutral_and_zero_confidence():
    result = funding.score(None, CFG)
    assert result["score"] == 0.0
    assert result["confidence"] == 0.0


def test_extreme_positive_funding_scores_bearish_with_high_confidence():
    # 0.05% per interval == the configured "extreme" threshold
    result = funding.score(0.0005, CFG)
    assert result["score"] == -1.0
    assert result["confidence"] == 1.0


def test_extreme_negative_funding_scores_bullish_with_high_confidence():
    result = funding.score(-0.0005, CFG)
    assert result["score"] == 1.0
    assert result["confidence"] == 1.0


def test_near_zero_funding_has_low_confidence():
    result = funding.score(0.00001, CFG)
    assert abs(result["score"]) < 0.3
    assert result["confidence"] < 0.3


def test_funding_rate_passed_through_for_visibility():
    result = funding.score(0.0002, CFG)
    assert result["funding_rate"] == 0.0002
