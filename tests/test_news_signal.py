"""NewsSignal.score_for_symbol: no longer falls back to the general article
pool when nothing symbol-specific matched -- for a universe-scanned altcoin,
"general market mood" is really just whatever's trending (usually BTC/ETH
headlines) and has no real bearing on that specific coin, so unrelated macro
news shouldn't be able to push its direction. Zero confidence (not a
substituted score) means zero influence on the aggregate.
"""
import time
from datetime import timedelta
from unittest.mock import MagicMock

from bot.signals.news import NewsSignal, _fetch_rss


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


def test_fetch_rss_uses_a_bounded_timeout_and_skips_a_failing_feed(monkeypatch):
    # feedparser.parse(url) fetches the URL itself with no timeout -- a single
    # unresponsive feed could hang the whole call forever. _fetch_rss must
    # fetch via requests (which has its own timeout) first, so a slow/failing
    # feed only costs a bounded wait and never blocks the other feeds.
    calls = []

    def fake_get(url, timeout=None, headers=None):
        calls.append((url, timeout))
        if url == "https://bad.example/rss":
            raise TimeoutError("simulated hang/timeout")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.content = b"<rss><channel><item><title>Good feed article</title></item></channel></rss>"
        return resp

    monkeypatch.setattr("bot.signals.news.requests.get", fake_get)

    articles = _fetch_rss(["https://bad.example/rss", "https://good.example/rss"], timedelta(hours=12))

    assert calls == [("https://bad.example/rss", 10), ("https://good.example/rss", 10)]
    assert len(articles) == 1
    assert articles[0]["title"] == "Good feed article"
