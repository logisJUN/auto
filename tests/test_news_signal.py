"""NewsSignal.score_for_symbol: no longer falls back to the general article
pool when nothing symbol-specific matched -- for a universe-scanned altcoin,
"general market mood" is really just whatever's trending (usually BTC/ETH
headlines) and has no real bearing on that specific coin, so unrelated macro
news shouldn't be able to push its direction. Zero confidence (not a
substituted score) means zero influence on the aggregate.
"""
import time

from bot.signals.news import NewsSignal


def _primed(articles: list[dict]) -> NewsSignal:
    signal = NewsSignal(cfg={}, newsapi_key=None)
    signal._articles = articles
    signal._cache_time = time.time()  # cache looks fresh -- _refresh_if_needed won't hit the network
    return signal


def test_no_fallback_when_nothing_symbol_specific_matches():
    articles = [
        {"title": "Bitcoin surges to new all-time high", "text": "Bitcoin surges to new all-time high", "ts": None},
        {"title": "Ethereum upgrade goes live", "text": "Ethereum upgrade goes live", "ts": None},
    ]
    signal = _primed(articles)

    result = signal.score_for_symbol("SOMEALTUSDT")

    assert result["score"] == 0.0
    assert result["confidence"] == 0.0
    assert result["article_count"] == 0
    assert result["sample"] == []


def test_relevant_articles_are_scored_and_others_excluded():
    articles = [
        {"title": "Bitcoin surges to new all-time high", "text": "Bitcoin surges to new all-time high", "ts": None},
        {"title": "Random altcoin news", "text": "Some unrelated coin news", "ts": None},
    ]
    signal = _primed(articles)

    result = signal.score_for_symbol("BTCUSDT")

    assert result["article_count"] == 1  # only the bitcoin-relevant article counted
    assert result["score"] > 0  # "surges", "all-time high" are bullish hints
    assert result["confidence"] > 0


def test_no_articles_at_all_is_zero_confidence():
    signal = _primed([])
    result = signal.score_for_symbol("BTCUSDT")
    assert result == {"score": 0.0, "confidence": 0.0, "article_count": 0, "sample": [],
                       "bullish_count": 0, "bearish_count": 0, "neutral_count": 0}


def test_articles_are_split_into_bullish_bearish_neutral_counts():
    articles = [
        {"title": "Bitcoin surges to new all-time high", "text": "Bitcoin surges to new all-time high", "ts": None},
        {"title": "Bitcoin crash sparks sell-off", "text": "Bitcoin crash sparks sell-off", "ts": None},
        {"title": "Bitcoin price holds steady", "text": "Bitcoin price holds steady", "ts": None},
    ]
    signal = _primed(articles)

    result = signal.score_for_symbol("BTCUSDT")

    assert result["article_count"] == 3
    assert result["bullish_count"] == 1   # "surges", "all-time high"
    assert result["bearish_count"] == 1   # "crash", "sell-off"
    assert result["neutral_count"] == 1   # no bullish/bearish keyword hits
    assert result["bullish_count"] + result["bearish_count"] + result["neutral_count"] == result["article_count"]
