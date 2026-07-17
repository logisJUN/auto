"""News sentiment signal.

Pulls crypto/macro headlines from free RSS feeds (and optionally NewsAPI.org if a
key is configured), scores each headline with a small bullish/bearish keyword
lexicon, and produces a symbol-aware score in [-1, 1] plus a confidence value
based on how many relevant, recent articles were found.

This is intentionally simple (no ML model, no paid sentiment API) so it has zero
mandatory external dependencies beyond RSS. It is one of several signals fed into
aggregator.py -- it is not meant to be predictive on its own.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

logger = logging.getLogger("bot.signals.news")

BULLISH_WORDS = [
    "surge", "rally", "soar", "soars", "bullish", "record high", "all-time high",
    "adoption", "approval", "approved", "etf inflow", "inflows", "breakout",
    "upgrade", "partnership", "rebound", "outperform", "institutional buying",
    "accumulate", "accumulation", "buy the dip", "rate cut", "dovish",
]
BEARISH_WORDS = [
    "crash", "plunge", "plunges", "bearish", "sell-off", "selloff", "hack",
    "hacked", "exploit", "lawsuit", "sues", "sued", "ban", "banned", "crackdown",
    "liquidation", "liquidated", "dump", "dumps", "recession", "default",
    "fraud", "collapse", "downgrade", "outflow", "outflows", "fear", "hawkish",
    "rate hike", "bankruptcy", "delisted", "investigation",
]

SYMBOL_KEYWORDS = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth", "ether"],
    "SOLUSDT": ["solana", "sol"],
    "XRPUSDT": ["xrp", "ripple"],
    "BNBUSDT": ["bnb", "binance coin"],
    "DOGEUSDT": ["dogecoin", "doge"],
}


def _symbol_keywords(symbol: str) -> list[str]:
    return SYMBOL_KEYWORDS.get(symbol.upper(), [symbol.upper().replace("USDT", "").lower()])


def _score_text(text: str) -> int:
    text = text.lower()
    score = 0
    for w in BULLISH_WORDS:
        if w in text:
            score += 1
    for w in BEARISH_WORDS:
        if w in text:
            score -= 1
    return score


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime.fromtimestamp(time.mktime(val), tz=timezone.utc)
    return None


def _fetch_rss(feeds: list[str], lookback: timedelta) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - lookback
    articles = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # feedparser rarely raises but be safe
            logger.warning("failed to parse feed %s: %s", url, exc)
            continue
        for entry in parsed.entries:
            ts = _entry_time(entry)
            if ts and ts < cutoff:
                continue
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            articles.append({"title": title, "text": f"{title} {summary}", "ts": ts})
    return articles


def _fetch_newsapi(api_key: str, query: str, lookback: timedelta, page_size: int = 30) -> list[dict]:
    since = (datetime.now(timezone.utc) - lookback).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query, "from": since, "sortBy": "publishedAt",
                "language": "en", "pageSize": page_size, "apiKey": api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("newsapi fetch failed: %s", exc)
        return []
    out = []
    for a in data.get("articles", []):
        title = a.get("title") or ""
        desc = a.get("description") or ""
        out.append({"title": title, "text": f"{title} {desc}", "ts": None})
    return out


class NewsSignal:
    """Caches fetched articles for `refresh_minutes` so we don't hammer feeds every tick."""

    def __init__(self, cfg: dict, newsapi_key: str | None):
        self.feeds = cfg.get("feeds", [])
        self.refresh_seconds = cfg.get("refresh_minutes", 15) * 60
        self.lookback = timedelta(hours=cfg.get("lookback_hours", 12))
        self.max_articles = cfg.get("max_articles", 60)
        self.newsapi_key = newsapi_key
        self._cache_time = 0.0
        self._articles: list[dict] = []

    def _refresh_if_needed(self):
        now = time.time()
        if now - self._cache_time < self.refresh_seconds and self._articles:
            return
        articles = _fetch_rss(self.feeds, self.lookback)
        if self.newsapi_key:
            articles += _fetch_newsapi(self.newsapi_key, "bitcoin OR ethereum OR crypto", self.lookback)
        # dedupe by title, cap length
        seen = set()
        deduped = []
        for a in articles:
            key = a["title"].strip().lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(a)
        self._articles = deduped[: self.max_articles]
        self._cache_time = now
        logger.info("news refreshed: %d articles", len(self._articles))

    def score_for_symbol(self, symbol: str) -> dict:
        self._refresh_if_needed()
        keywords = _symbol_keywords(symbol)
        relevant = [a for a in self._articles if any(k in a["text"].lower() for k in keywords)]
        # if nothing symbol-specific, fall back to general market mood (macro affects all of crypto)
        pool = relevant if relevant else self._articles

        if not pool:
            return {"score": 0.0, "confidence": 0.0, "article_count": 0, "sample": []}

        raw_scores = [_score_text(a["text"]) for a in pool]
        avg = sum(raw_scores) / len(raw_scores)
        score = max(-1.0, min(1.0, avg / 2.5))
        confidence = max(0.0, min(1.0, len(pool) / 15))

        sample = [a["title"] for a in pool[:5]]
        return {"score": score, "confidence": confidence, "article_count": len(pool), "sample": sample}
