"""SEC EDGAR real-time 8-K filing detector.

Company-level material events (new contracts, M&A, executive changes,
bankruptcy, earnings results, etc.) that a US public company is legally
required to disclose promptly show up here, often before -- sometimes well
before -- any news article is written about them.

This is intentionally NOT blended into aggressive_btc's quantitative
blended score the way NewsSignal is. An 8-K filing's own title/metadata
carries no sentiment -- it only says a filing of a given type happened, not
whether the news in it is good or bad. Reliably extracting real sentiment
would mean parsing the filing's actual exhibit documents (e.g. an earnings
press release buried inside the filing), which is real additional scope
left for later. For now this only detects + notifies (Telegram, plus a
decisions.jsonl "edgar_filing" row for the dashboard) the instant a tracked
symbol's company files an 8-K -- the fastest "catch" available without
trying to automatically judge the filing's content.

Uses two free, unauthenticated SEC EDGAR endpoints:
  - https://www.sec.gov/files/company_tickers.json: a static ticker->CIK
    lookup table, SEC's own canonical mapping. Refetched rarely (see
    TICKER_CIK_REFRESH_HOURS) since this almost never changes.
  - https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&
    output=atom: an Atom feed of the most recently filed 8-Ks SITE-WIDE
    (not per-company) -- ONE request covers every tracked symbol at once,
    instead of one request per symbol (which would mean dozens of
    sequential SEC requests per refresh and risk SEC's fair-access rate
    limits for no real benefit).

SEC requires a descriptive User-Agent on every automated request (no API
key, but a blank/generic one gets rate-limited/blocked harder) -- see
SEC_EDGAR_CONTACT_EMAIL below.

NOTE: this was built in a sandboxed development session that cannot reach
sec.gov to verify these endpoints live (same network restriction noted
elsewhere in this codebase for api.bybit.com) -- the request/parsing logic
is covered by tests with mocked HTTP responses, but the very first real
check of this against SEC's actual servers happens after this deploys.
Watch Render's logs for "edgar:" warnings after it ships; if the ticker->CIK
map never resolves anything or the feed fetch keeps failing, the endpoint
shapes assumed here may have drifted from SEC's actual current behavior.
"""
from __future__ import annotations

import logging
import os
import re
import time

import feedparser
import requests

logger = logging.getLogger("bot.signals.edgar")

TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
CURRENT_8K_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&company=&dateb=&owner=include&count=100&output=atom"
)
TICKER_CIK_REFRESH_HOURS = 24
REQUEST_TIMEOUT_SEC = 10
# A caller should keep seen-link memory bounded -- the feed only ever shows
# the most recent ~100 site-wide filings, so there's never a real need to
# remember more than a few hundred links.
_MAX_SEEN_LINKS = 2000
_SEEN_LINKS_TRIM_TO = 1000

_CIK_RE = re.compile(r"CIK=(\d+)")


def _user_agent() -> str:
    # SEC blocks/throttles requests without a descriptive UA identifying the
    # requester -- a contact email is the documented convention. Configurable
    # via env var so no one's personal email is hardcoded into this repo;
    # falls back to a generic placeholder if unset (works, but SEC may
    # rate-limit a generic/shared UA harder than one with a real contact --
    # set SEC_EDGAR_CONTACT_EMAIL in .env if this starts getting throttled).
    contact = os.getenv("SEC_EDGAR_CONTACT_EMAIL", "contact@example.com")
    return f"aggressive_btc-bot {contact}"


def _extract_cik(link: str) -> str:
    """Pulls the zero-padded 10-digit CIK out of an EDGAR filing-index link
    like ".../cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&...".
    Returns "" if the link doesn't contain one.
    """
    match = _CIK_RE.search(link)
    return match.group(1).zfill(10) if match else ""


class EdgarFilingWatcher:
    """Tracks the most recent 8-K filings seen for a fixed set of tracked
    tickers, polled via SEC's site-wide "current events" feed. Call
    check_for_new_filings() periodically (see aggressive_btc.main()'s use of
    this) -- returns only entries not already returned by a previous call on
    this same instance, so a caller can notify exactly once per real filing.
    Never raises; any failure here is observability-only and must not be
    able to affect trading.
    """

    def __init__(self, tracked_tickers: set[str]):
        self.tracked_tickers = {t.upper() for t in tracked_tickers}
        self._cik_to_ticker: dict[str, str] = {}
        self._cik_cache_time = 0.0
        self._seen_links: set[str] = set()

    def _refresh_cik_map_if_needed(self):
        now = time.time()
        if now - self._cik_cache_time < TICKER_CIK_REFRESH_HOURS * 3600 and self._cik_to_ticker:
            return
        try:
            resp = requests.get(TICKER_CIK_URL, headers={"User-Agent": _user_agent()},
                                 timeout=REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning("edgar: failed to refresh ticker->CIK map, keeping previous (if any)",
                            exc_info=True)
            return
        mapping = {}
        for row in data.values():
            ticker = str(row.get("ticker", "")).upper()
            if ticker not in self.tracked_tickers:
                continue
            cik = str(row.get("cik_str", "")).zfill(10)
            mapping[cik] = ticker
        if mapping:
            self._cik_to_ticker = mapping
            self._cik_cache_time = now
            logger.info("edgar: refreshed ticker->CIK map, resolved %d of %d tracked tickers",
                        len(mapping), len(self.tracked_tickers))
        else:
            logger.warning("edgar: ticker->CIK refresh returned no matches for any tracked ticker")

    def check_for_new_filings(self) -> list[dict]:
        """Returns a list of {"ticker", "title", "link"} for every 8-K a
        tracked ticker's company has filed since the last call that hasn't
        already been returned. Empty list on any failure (CIK map not
        resolved yet, feed fetch failed, feed unparseable) -- this is a
        best-effort detector, not a guaranteed one.
        """
        self._refresh_cik_map_if_needed()
        if not self._cik_to_ticker:
            return []
        try:
            resp = requests.get(CURRENT_8K_FEED_URL, headers={"User-Agent": _user_agent()},
                                 timeout=REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception:
            logger.warning("edgar: failed to fetch the current 8-K feed", exc_info=True)
            return []

        new_filings = []
        for entry in parsed.entries:
            link = entry.get("link", "")
            if not link or link in self._seen_links:
                continue
            ticker = self._cik_to_ticker.get(_extract_cik(link))
            if ticker is None:
                continue
            self._seen_links.add(link)
            new_filings.append({"ticker": ticker, "title": entry.get("title", ""), "link": link})

        if len(self._seen_links) > _MAX_SEEN_LINKS:
            self._seen_links = set(list(self._seen_links)[-_SEEN_LINKS_TRIM_TO:])
        return new_filings
