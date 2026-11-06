"""bot/newsfeed.py -- pure-logic tests plus DB-flow tests against a fake
psycopg2 connection/cursor (no real Postgres available in this sandbox; see
the module's own docstring on why DATABASE_URL is required in production).
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from bot import newsfeed
from bot.exchange.bybit_client import BybitAPIError
from bot.signals.news import NewsSignal

# Captured before the autouse fixture below stubs it out for every other
# test -- the _fetch_article_body-specific tests restore the real function
# with this reference so they can actually exercise it.
_REAL_FETCH_ARTICLE_BODY = newsfeed._fetch_article_body


class FakeCursor:
    def __init__(self, calls, fetchone_queue, fetchall_queue):
        self.calls = calls
        self.fetchone_queue = fetchone_queue
        self.fetchall_queue = fetchall_queue
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        if "UPDATE" in sql:
            self.rowcount = 1

    def fetchone(self):
        return self.fetchone_queue.pop(0) if self.fetchone_queue else None

    def fetchall(self):
        return self.fetchall_queue.pop(0) if self.fetchall_queue else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, fetchone_queue=None, fetchall_queue=None):
        self.calls: list = []
        self.fetchone_queue = fetchone_queue or []
        self.fetchall_queue = fetchall_queue or []
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self.calls, self.fetchone_queue, self.fetchall_queue)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(newsfeed, "ENABLED", True)
    monkeypatch.setattr(newsfeed, "DATABASE_URL", "postgres://fake")
    monkeypatch.setattr(newsfeed, "_schema_ready", False)
    # No real HTTP fetch in these tests by default -- see the dedicated
    # _fetch_article_body tests below for its own network-mocked behavior.
    monkeypatch.setattr(newsfeed, "_fetch_article_body", lambda link: None)
    yield


def _article(ts=None, title="Apple stock surges after strong earnings report", link="https://x/1", summary="sum"):
    return {"title": title, "text": title, "ts": ts or datetime.now(timezone.utc),
            "link": link, "summary": summary, "source": "Example Feed"}


# -- pure logic --------------------------------------------------------

def test_direction_from_score():
    assert newsfeed._direction_from_score(3) == "up"
    assert newsfeed._direction_from_score(-2) == "down"
    assert newsfeed._direction_from_score(0) == "neutral"


def test_confidence_from_score_is_clamped_to_one():
    assert newsfeed._confidence_from_score(0) == 0.0
    assert newsfeed._confidence_from_score(5) == 1.0
    assert newsfeed._confidence_from_score(100) == 1.0


def test_category_for_detects_earnings_keywords():
    assert newsfeed._category_for("Company beats earnings estimates") == "earnings"
    assert newsfeed._category_for("Company signs new supply contract") == "news"


def test_article_id_is_stable_and_keyed_by_symbol_and_link():
    a = newsfeed._article_id("AAPLUSDT", "https://x/1", "title")
    b = newsfeed._article_id("AAPLUSDT", "https://x/1", "title")
    c = newsfeed._article_id("TSLAUSDT", "https://x/1", "title")
    assert a == b
    assert a != c


def test_disabled_functions_are_all_safe_noops(monkeypatch):
    monkeypatch.setattr(newsfeed, "ENABLED", False)
    client = MagicMock()
    assert newsfeed.save_article_if_new("AAPLUSDT", _article(), client) is False
    assert newsfeed.save_filing_article("AAPLUSDT", {"ticker": "AAPL", "title": "8-K", "link": "x"}, client) is False
    assert newsfeed.set_user_prediction("abc", "up") is False
    assert newsfeed.resolve_due_articles(client) == 0
    assert newsfeed.list_recent_articles() == []
    assert newsfeed.get_article("abc") is None
    stats = newsfeed.get_accuracy_stats()
    assert stats["ai_judged"] == 0 and stats["breakdown"] == []


def test_save_article_if_new_skips_articles_with_no_published_time(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    article = _article()
    article["ts"] = None

    result = newsfeed.save_article_if_new("AAPLUSDT", article, client)

    assert result is False
    assert conn.calls == []  # never even connects to check for a duplicate


def test_save_article_if_new_inserts_and_fetches_price_at_publish(monkeypatch):
    conn = FakeConn(fetchone_queue=[None])  # no existing row
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    client.get_klines.return_value = [{"close": 123.45}]

    result = newsfeed.save_article_if_new("AAPLUSDT", _article(), client)

    assert result is True
    assert conn.committed is True
    insert_calls = [c for c in conn.calls if "INSERT INTO" in c[0]]
    assert len(insert_calls) == 1
    params = insert_calls[0][1]
    assert params[1] == "AAPLUSDT"  # symbol
    assert params[9] == 123.45      # price_at_publish
    assert params[10] == "up"       # "beats earnings estimates" scores bullish
    assert params[7] == "earnings"  # category auto-detected


def test_save_article_if_new_skips_duplicate_without_fetching_price(monkeypatch):
    conn = FakeConn(fetchone_queue=[(1,)])  # row already exists
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()

    result = newsfeed.save_article_if_new("AAPLUSDT", _article(), client)

    assert result is False
    client.get_klines.assert_not_called()
    assert not any("INSERT INTO" in c[0] for c in conn.calls)


def test_save_article_if_new_stores_the_fetched_article_body(monkeypatch):
    conn = FakeConn(fetchone_queue=[None])
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    monkeypatch.setattr(newsfeed, "_fetch_article_body", lambda link: "Full article text here.")
    client = MagicMock()
    client.get_klines.return_value = [{"close": 1.0}]

    newsfeed.save_article_if_new("AAPLUSDT", _article(), client)

    insert_calls = [c for c in conn.calls if "INSERT INTO" in c[0]]
    assert insert_calls[0][1][4] == "Full article text here."  # body_text


def test_save_article_if_new_still_saves_when_price_lookup_fails(monkeypatch):
    conn = FakeConn(fetchone_queue=[None])
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    client.get_klines.side_effect = BybitAPIError("boom")

    result = newsfeed.save_article_if_new("AAPLUSDT", _article(), client)

    assert result is True
    insert_calls = [c for c in conn.calls if "INSERT INTO" in c[0]]
    assert insert_calls[0][1][9] is None  # price_at_publish


def test_save_filing_article_uses_neutral_direction_and_zero_confidence(monkeypatch):
    conn = FakeConn(fetchone_queue=[None])
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    client.get_klines.return_value = [{"close": 10.0}]

    result = newsfeed.save_filing_article(
        "JPMUSDT", {"ticker": "JPM", "title": "8-K - JPMorgan Chase", "link": "https://sec.gov/x"}, client)

    assert result is True
    insert_calls = [c for c in conn.calls if "INSERT INTO" in c[0]]
    params = insert_calls[0][1]
    assert params[7] == "filing"
    assert params[10] == "neutral"
    assert params[11] == 0.0
    assert params[4] is None  # body_text -- filings never get a body fetch


def test_collect_from_news_cache_only_saves_symbols_whose_keywords_match(monkeypatch):
    saved = []
    monkeypatch.setattr(newsfeed, "save_article_if_new", lambda symbol, article, client: saved.append(symbol))
    news_signal = MagicMock(spec=NewsSignal)
    news_signal.all_articles.return_value = [
        {"title": "Apple unveils new iPhone", "text": "Apple unveils new iPhone", "ts": datetime.now(timezone.utc),
         "link": "x", "summary": "", "source": ""},
        {"title": "Unrelated market chatter", "text": "Unrelated market chatter", "ts": datetime.now(timezone.utc),
         "link": "y", "summary": "", "source": ""},
    ]
    news_cache = {1: news_signal}
    symbols_by_news_key = {1: ["AAPLUSDT", "TSLAUSDT"]}

    newsfeed.collect_from_news_cache(news_cache, symbols_by_news_key, MagicMock())

    assert saved == ["AAPLUSDT"]


def test_set_user_prediction_rejects_invalid_direction(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    assert newsfeed.set_user_prediction("abc", "sideways") is False
    assert conn.calls == []


def test_set_user_prediction_succeeds_when_row_is_updated(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    assert newsfeed.set_user_prediction("abc", "up") is True
    assert conn.committed is True


def test_resolve_due_articles_marks_correct_predictions(monkeypatch):
    now = time.time()
    rows = [("id1", "AAPLUSDT", 100.0, "up", "up", now - 1)]
    conn = FakeConn(fetchall_queue=[rows])
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    client.get_klines.return_value = [{"close": 110.0}]  # price went up

    resolved = newsfeed.resolve_due_articles(client)

    assert resolved == 1
    update_calls = [c for c in conn.calls if c[0].startswith("UPDATE")]
    assert len(update_calls) == 1
    price_after, actual_direction, ai_correct, user_correct, article_id = update_calls[0][1]
    assert price_after == 110.0
    assert actual_direction == "up"
    assert ai_correct is True
    assert user_correct is True
    assert article_id == "id1"


def test_resolve_due_articles_leaves_row_pending_when_price_lookup_fails(monkeypatch):
    now = time.time()
    rows = [("id1", "AAPLUSDT", 100.0, "up", None, now - 1)]
    conn = FakeConn(fetchall_queue=[rows])
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    client.get_klines.side_effect = BybitAPIError("boom")

    resolved = newsfeed.resolve_due_articles(client)

    assert resolved == 0
    assert not any(c[0].startswith("UPDATE") for c in conn.calls)


def test_resolve_due_articles_skips_direction_judgement_without_a_publish_price(monkeypatch):
    now = time.time()
    rows = [("id1", "AAPLUSDT", None, "up", "down", now - 1)]
    conn = FakeConn(fetchall_queue=[rows])
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)
    client = MagicMock()
    client.get_klines.return_value = [{"close": 110.0}]

    resolved = newsfeed.resolve_due_articles(client)

    assert resolved == 1
    update_calls = [c for c in conn.calls if c[0].startswith("UPDATE")]
    price_after, actual_direction, ai_correct, user_correct, article_id = update_calls[0][1]
    assert actual_direction is None
    assert ai_correct is None
    assert user_correct is None


def test_get_accuracy_stats_shapes_breakdown_rows(monkeypatch):
    conn = FakeConn(fetchall_queue=[
        [(10, 7, 10, 4)],  # ai_judged, ai_correct, user_judged, user_correct -- actually fetchone below
    ])
    # First query result comes from fetchone, second from fetchall -- set both explicitly.
    conn.fetchone_queue = [(10, 7, 10, 4)]
    conn.fetchall_queue = [[("earnings", "up", "down", 3)]]
    monkeypatch.setattr(newsfeed, "_get_conn", lambda: conn)

    stats = newsfeed.get_accuracy_stats()

    assert stats["ai_judged"] == 10
    assert stats["ai_correct"] == 7
    assert stats["ai_accuracy_pct"] == 70.0
    assert stats["user_accuracy_pct"] == 40.0
    assert stats["breakdown"] == [{"category": "earnings", "ai_direction": "up",
                                    "actual_direction": "down", "count": 3}]


# -- _fetch_article_body --------------------------------------------------------

def test_fetch_article_body_returns_none_for_an_empty_link(monkeypatch):
    monkeypatch.setattr(newsfeed, "_fetch_article_body", _REAL_FETCH_ARTICLE_BODY)
    assert newsfeed._fetch_article_body("") is None


def test_fetch_article_body_returns_none_when_the_request_fails(monkeypatch):
    monkeypatch.setattr(newsfeed, "_fetch_article_body", _REAL_FETCH_ARTICLE_BODY)
    def fake_get(url, timeout=None, headers=None):
        raise TimeoutError("simulated hang/timeout")
    monkeypatch.setattr(newsfeed.requests, "get", fake_get)

    assert newsfeed._fetch_article_body("https://example.com/story") is None


def test_fetch_article_body_extracts_and_truncates_long_text(monkeypatch):
    monkeypatch.setattr(newsfeed, "_fetch_article_body", _REAL_FETCH_ARTICLE_BODY)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.text = "<html><body><article><p>hello world</p></article></body></html>"
    monkeypatch.setattr(newsfeed.requests, "get", lambda *a, **k: resp)
    monkeypatch.setattr(newsfeed, "ARTICLE_BODY_MAX_CHARS", 5)
    fake_trafilatura = MagicMock()
    fake_trafilatura.extract.return_value = "hello world"
    monkeypatch.setitem(sys.modules, "trafilatura", fake_trafilatura)

    body = newsfeed._fetch_article_body("https://example.com/story")

    assert body == "hello"  # truncated to ARTICLE_BODY_MAX_CHARS


def test_fetch_article_body_returns_none_when_extraction_finds_nothing(monkeypatch):
    monkeypatch.setattr(newsfeed, "_fetch_article_body", _REAL_FETCH_ARTICLE_BODY)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.text = "<html><body></body></html>"
    monkeypatch.setattr(newsfeed.requests, "get", lambda *a, **k: resp)
    fake_trafilatura = MagicMock()
    fake_trafilatura.extract.return_value = None
    monkeypatch.setitem(sys.modules, "trafilatura", fake_trafilatura)

    assert newsfeed._fetch_article_body("https://example.com/story") is None


# -- collect_from_news_cache per-cycle cap --------------------------------------------------------

def test_collect_from_news_cache_stops_once_the_per_cycle_cap_is_reached(monkeypatch):
    monkeypatch.setattr(newsfeed, "MAX_NEW_ARTICLES_PER_COLLECT_CYCLE", 2)
    saved = []
    monkeypatch.setattr(newsfeed, "save_article_if_new", lambda symbol, article, client: saved.append(symbol) or True)
    news_signal = MagicMock(spec=NewsSignal)
    news_signal.all_articles.return_value = [
        {"title": f"Apple story {i}", "text": f"Apple story {i}", "ts": datetime.now(timezone.utc),
         "link": f"x{i}", "summary": "", "source": ""}
        for i in range(5)
    ]
    news_cache = {1: news_signal}
    symbols_by_news_key = {1: ["AAPLUSDT"]}

    newsfeed.collect_from_news_cache(news_cache, symbols_by_news_key, MagicMock())

    assert len(saved) == 2
