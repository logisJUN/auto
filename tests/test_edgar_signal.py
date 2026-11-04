"""EdgarFilingWatcher: detects (and only detects -- see bot/signals/edgar.py's
module docstring for why this is notification-only, not scored) 8-K filings
for a fixed set of tracked tickers via SEC EDGAR's two free, unauthenticated
endpoints. Mocked at the requests.get layer (same pattern as
test_news_signal.py's _fetch_rss tests) -- feedparser itself parses real
Atom bytes, nothing about feedparser is mocked.
"""
import time
from unittest.mock import MagicMock

from bot.signals.edgar import EdgarFilingWatcher, _extract_cik, _user_agent

_TICKER_JSON = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

_ATOM_TWO_ENTRIES = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - Apple Inc. (0000320193) (Filer)</title>
    <link rel="alternate" type="text/html"
          href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000320193&amp;type=8-K"/>
    <updated>2026-11-04T12:00:00-05:00</updated>
  </entry>
  <entry>
    <title>8-K - Untracked Co (0000999999) (Filer)</title>
    <link rel="alternate" type="text/html"
          href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000999999&amp;type=8-K"/>
    <updated>2026-11-04T12:01:00-05:00</updated>
  </entry>
</feed>
"""


def _fake_get(ticker_json=_TICKER_JSON, feed_bytes=_ATOM_TWO_ENTRIES, ticker_exc=None, feed_exc=None):
    def fake_get(url, headers=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if "company_tickers.json" in url:
            if ticker_exc:
                raise ticker_exc
            resp.json.return_value = ticker_json
        else:
            if feed_exc:
                raise feed_exc
            resp.content = feed_bytes
        return resp
    return fake_get


def test_extract_cik_pulls_the_zero_padded_cik_from_a_filing_link():
    link = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=8-K"
    assert _extract_cik(link) == "0000320193"


def test_extract_cik_returns_empty_string_when_no_cik_in_link():
    assert _extract_cik("https://example.com/no-cik-here") == ""


def test_user_agent_includes_contact_email_env_var(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_CONTACT_EMAIL", "someone@example.com")
    assert "someone@example.com" in _user_agent()


def test_user_agent_falls_back_to_a_placeholder_when_unset(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_CONTACT_EMAIL", raising=False)
    assert "@" in _user_agent()  # some contact-shaped placeholder, not blank


def test_detects_a_new_filing_for_a_tracked_ticker_and_ignores_untracked(monkeypatch):
    monkeypatch.setattr("bot.signals.edgar.requests.get", _fake_get())
    watcher = EdgarFilingWatcher({"AAPL", "MSFT"})

    filings = watcher.check_for_new_filings()

    assert len(filings) == 1  # the untracked CIK 0000999999 entry is dropped
    assert filings[0]["ticker"] == "AAPL"
    assert "Apple" in filings[0]["title"]
    assert filings[0]["link"]


def test_does_not_return_the_same_filing_twice(monkeypatch):
    monkeypatch.setattr("bot.signals.edgar.requests.get", _fake_get())
    watcher = EdgarFilingWatcher({"AAPL", "MSFT"})

    first = watcher.check_for_new_filings()
    second = watcher.check_for_new_filings()  # same feed content again

    assert len(first) == 1
    assert second == []


def test_cik_map_refresh_failure_returns_empty_list_without_crashing(monkeypatch):
    monkeypatch.setattr("bot.signals.edgar.requests.get",
                         _fake_get(ticker_exc=ConnectionError("simulated network failure")))
    watcher = EdgarFilingWatcher({"AAPL"})

    assert watcher.check_for_new_filings() == []


def test_feed_fetch_failure_returns_empty_list_without_crashing(monkeypatch):
    monkeypatch.setattr("bot.signals.edgar.requests.get",
                         _fake_get(feed_exc=TimeoutError("simulated timeout")))
    watcher = EdgarFilingWatcher({"AAPL"})

    assert watcher.check_for_new_filings() == []


def test_untracked_ticker_set_never_resolves_any_cik_and_stays_empty(monkeypatch):
    monkeypatch.setattr("bot.signals.edgar.requests.get", _fake_get())
    watcher = EdgarFilingWatcher({"SOME_TICKER_NOT_IN_THE_SEC_LIST"})

    assert watcher.check_for_new_filings() == []


def test_cik_map_is_cached_and_not_refetched_within_the_ttl(monkeypatch):
    calls = []

    def counting_get(url, headers=None, timeout=None):
        calls.append(url)
        return _fake_get()(url, headers=headers, timeout=timeout)

    monkeypatch.setattr("bot.signals.edgar.requests.get", counting_get)
    watcher = EdgarFilingWatcher({"AAPL", "MSFT"})

    watcher.check_for_new_filings()
    ticker_calls_after_first = sum(1 for u in calls if "company_tickers.json" in u)
    watcher.check_for_new_filings()
    ticker_calls_after_second = sum(1 for u in calls if "company_tickers.json" in u)

    assert ticker_calls_after_first == 1
    assert ticker_calls_after_second == 1  # still cached -- no second CIK-map fetch


def test_cik_map_refetches_once_the_ttl_expires(monkeypatch):
    monkeypatch.setattr("bot.signals.edgar.requests.get", _fake_get())
    watcher = EdgarFilingWatcher({"AAPL", "MSFT"})
    watcher.check_for_new_filings()
    assert watcher._cik_cache_time > 0

    watcher._cik_cache_time = time.time() - 25 * 3600  # force past the 24h TTL
    calls = []

    def counting_get(url, headers=None, timeout=None):
        calls.append(url)
        return _fake_get()(url, headers=headers, timeout=timeout)

    monkeypatch.setattr("bot.signals.edgar.requests.get", counting_get)
    watcher.check_for_new_filings()

    assert any("company_tickers.json" in u for u in calls)
