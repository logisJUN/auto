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

import html
import logging
import re
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
    # SOXLUSDT is a tokenized 3x semiconductor ETF, not a coin -- relevant
    # news is about the sector/underlying, not anything literally called "soxl".
    "SOXLUSDT": ["semiconductor", "semiconductors", "chip stocks", "chipmaker",
                 "nvidia", "amd", "tsmc", "intel", "qualcomm", "broadcom",
                 "philadelphia semiconductor", "sox index", "export controls",
                 "chip ban", "chip export"],
    # Bybit's 1x tokenized-stock perpetuals (aggressive_btc's STOCK_CONFIGS/
    # SEMI_AI_CONFIGS) -- relevant news is about the company itself, not a
    # bare ticker-as-word match that would pick up unrelated articles (e.g.
    # not bare "meta"/"coin"/"intel"/"oracle", which collide with unrelated
    # usage -- "intel"/"oracle" especially, both common English words with
    # their own unrelated meanings in general news/crypto coverage).
    "AAPLUSDT": ["apple", "aapl", "iphone", "tim cook", "ipad", "macbook"],
    "TSLAUSDT": ["tesla", "tsla", "elon musk", "cybertruck", "model y", "model 3"],
    "AMZNUSDT": ["amazon", "amzn", "aws", "jeff bezos", "andy jassy"],
    "GOOGLUSDT": ["google", "alphabet", "googl", "sundar pichai", "youtube"],
    "METAUSDT": ["meta platforms", "facebook", "instagram", "zuckerberg",
                 "whatsapp", "reality labs"],
    "MSFTUSDT": ["microsoft", "msft", "satya nadella", "azure", "windows", "copilot"],
    "COINUSDT": ["coinbase", "brian armstrong", "coinbase global"],
    "MSTRUSDT": ["microstrategy", "mstr", "michael saylor", "strategy inc"],
    "ORCLUSDT": ["oracle corporation", "oracle cloud", "oracle ceo", "larry ellison", "safra catz"],
    "RKLBUSDT": ["rocket lab", "peter beck"],
    "RDWUSDT": ["redwire", "redwire space"],
    "RIVNUSDT": ["rivian", "rivian automotive"],
    "DKNGUSDT": ["draftkings"],
    "DJTUSDT": ["trump media", "truth social"],
    "COSTUSDT": ["costco", "costco wholesale"],
    "HDUSDT": ["home depot"],
    "MNSTUSDT": ["monster beverage"],
    "SONYUSDT": ["sony group", "sony corporation", "playstation"],
    # Semiconductor / AI-infrastructure pool (aggressive_btc.SEMI_AI_CONFIGS)
    "NVDAUSDT": ["nvidia", "nvda", "jensen huang", "gpu", "h100", "blackwell"],
    "AVGOUSDT": ["broadcom", "avgo", "hock tan"],
    "TSMUSDT": ["tsmc", "taiwan semiconductor"],
    "MUUSDT": ["micron", "micron technology"],
    "INTCUSDT": ["intel corporation", "intel corp", "intel foundry", "intel ceo", "lip-bu tan"],
    "AMATUSDT": ["applied materials"],
    "KLACUSDT": ["kla corporation", "kla corp"],
    "LRCXUSDT": ["lam research"],
    "MRVLUSDT": ["marvell", "marvell technology"],
    "ADIUSDT": ["analog devices"],
    "CRDOUSDT": ["credo technology"],
    "COHRUSDT": ["coherent corp", "coherent corporation"],
    "LITEUSDT": ["lumentum"],
    "CIENUSDT": ["ciena", "ciena corporation"],
    "ALABUSDT": ["astera labs"],
    "AEHRUSDT": ["aehr test systems"],
    "SMCIUSDT": ["super micro computer", "supermicro", "smci"],
    "DELLUSDT": ["dell technologies", "dell inc", "michael dell", "dell "],
    "HPEUSDT": ["hewlett packard enterprise"],
    "ANETUSDT": ["arista networks"],
    # Added to SEMI_AI_CONFIGS from the S&P500-vs-Bybit cross-check --
    # semiconductor/chip-equipment/storage names.
    "GLWUSDT": ["corning inc", "corning incorporated"],
    "ONUSDT": ["on semiconductor", "onsemi"],
    "QUSDT": ["qnity electronics", "qnity"],
    "QCOMUSDT": ["qualcomm"],
    "SNDKUSDT": ["sandisk"],
    "SNPSUSDT": ["synopsys"],
    "STXUSDT": ["seagate technology", "seagate"],
    "TERUSDT": ["teradyne"],
    "TXNUSDT": ["texas instruments"],
    "WDCUSDT": ["western digital"],
    # Added to STOCK_CONFIGS from the same cross-check -- general software/
    # enterprise-tech and communication-services names.
    "ACNUSDT": ["accenture"],
    "ADBEUSDT": ["adobe inc", "adobe systems"],
    "AKAMUSDT": ["akamai technologies", "akamai"],
    "CRMUSDT": ["salesforce", "marc benioff"],
    "CRWDUSDT": ["crowdstrike"],
    "CSCOUSDT": ["cisco systems", "cisco"],
    "DDOGUSDT": ["datadog"],
    "FLEXUSDT": ["flex ltd", "flextronics"],
    "IBMUSDT": ["ibm", "international business machines"],
    "INTUUSDT": ["intuit"],
    "NOWUSDT": ["servicenow"],
    "PANWUSDT": ["palo alto networks"],
    "PLTRUSDT": ["palantir"],
    "NFLXUSDT": ["netflix"],
    "RDDTUSDT": ["reddit inc", "reddit ipo"],
    # TUSDT (AT&T) excluded -- see aggressive_btc.STOCK_SYMBOLS's comment.
    "TTWOUSDT": ["take-two interactive", "take two interactive"],
    # Third pool, FINANCE_CONFIGS -- banks/payments/brokers/insurers.
    "AXPUSDT": ["american express"],
    "BACUSDT": ["bank of america"],
    "BRKBUSDT": ["berkshire hathaway", "warren buffett"],
    "BXUSDT": ["blackstone inc", "blackstone group"],
    "CUSDT": ["citigroup", "citibank"],
    "CFGUSDT": ["citizens financial group", "citizens bank"],
    "FISUSDT": ["fidelity national information services", "fis global"],
    "GSUSDT": ["goldman sachs"],
    "HOODUSDT": ["robinhood markets", "robinhood app", "vlad tenev"],
    "IBKRUSDT": ["interactive brokers"],
    "JPMUSDT": ["jpmorgan", "jp morgan", "jamie dimon"],
    # KEYUSDT (KeyCorp) excluded -- see aggressive_btc.FINANCE_SYMBOLS's comment.
    "MAUSDT": ["mastercard"],
    "METUSDT": ["metlife"],
    "PYPLUSDT": ["paypal"],
    "VUSDT": ["visa inc", "visa card"],
    # Fourth pool, ENERGY_CONFIGS -- oil/gas majors. CVXUSDT (Chevron)
    # deliberately excluded -- see aggressive_btc.ENERGY_SYMBOLS's comment:
    # that symbol is actually Convex Finance's crypto token, not Chevron.
    "EQTUSDT": ["eqt corporation", "eqt corp"],
    "OXYUSDT": ["occidental petroleum"],
    "XOMUSDT": ["exxonmobil", "exxon mobil", "exxon"],
}


def _symbol_keywords(symbol: str) -> list[str]:
    return SYMBOL_KEYWORDS.get(symbol.upper(), [symbol.upper().replace("USDT", "").lower()])


def symbol_keywords(symbol: str) -> list[str]:
    """Public wrapper around _symbol_keywords -- reused by bot/newsfeed.py to
    figure out which of a shared news pool's articles are actually about a
    given symbol, the same way score_for_symbol does internally.
    """
    return _symbol_keywords(symbol)


def score_text(text: str) -> int:
    """Public wrapper around the keyword-lexicon scorer below -- reused by
    bot/newsfeed.py for per-article up/down inference, separate from the
    aggregated per-symbol score this module normally produces.
    """
    return _score_text(text)


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


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_summary(entry, title: str) -> str:
    """Some feeds (observed live: a "Stock Market News"-sourced article with
    no visible summary on the dashboard) put nothing in <description> at
    all, or put it under <content:encoded> instead -- feedparser exposes
    that as entry.content[0].value, which <description>-only code never
    checked. Falls back to that, strips any HTML markup either field may
    contain (so the dashboard shows readable text, not raw tags), and drops
    a result that's just the title repeated back (common for feeds whose
    <description> is title-only) -- all of those end up "" here, which the
    dashboard already renders as "no summary available" rather than hiding
    the section with no explanation.
    """
    raw = entry.get("summary", "") or ""
    if not raw.strip():
        content = entry.get("content") or []
        if content:
            raw = content[0].get("value", "") or ""
    text = html.unescape(_HTML_TAG_RE.sub(" ", raw))
    text = " ".join(text.split())
    if not text or text.strip().lower() == title.strip().lower():
        return ""
    return text


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime.fromtimestamp(time.mktime(val), tz=timezone.utc)
    return None


def _fetch_rss(feeds: list[str], lookback: timedelta) -> list[dict]:
    """feedparser.parse(url) fetches the URL itself with NO timeout -- a single
    unresponsive feed (connection accepted, server just never replies) hangs
    this forever with no exception raised, which silently freezes the entire
    calling tick loop (observed live: aggressive_btc went dead silent for
    90+ minutes, no errors, no further log lines, after RSS feeds were
    expanded to 10). Fetching with requests' own timeout first and handing
    feedparser the already-downloaded bytes avoids ever blocking inside
    feedparser itself.
    """
    cutoff = datetime.now(timezone.utc) - lookback
    articles = []
    for url in feeds:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            logger.warning("failed to fetch/parse feed %s: %s", url, exc)
            continue
        feed_title = parsed.feed.get("title", "") if hasattr(parsed, "feed") else ""
        for entry in parsed.entries:
            ts = _entry_time(entry)
            if ts and ts < cutoff:
                continue
            title = entry.get("title", "")
            # Raw (un-cleaned) summary for scoring/keyword-matching text --
            # score_for_symbol's keyword matching doesn't care about stray
            # HTML tags, so this stays exactly as before. _clean_summary's
            # output (see its own docstring) is only for display.
            raw_summary = entry.get("summary", "")
            articles.append({
                "title": title, "text": f"{title} {raw_summary}", "ts": ts,
                "link": entry.get("link", ""), "summary": _clean_summary(entry, title), "source": feed_title,
            })
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
        ts = None
        published_at = a.get("publishedAt")
        if published_at:
            try:
                ts = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
        source = (a.get("source") or {}).get("name", "")
        clean_desc = html.unescape(_HTML_TAG_RE.sub(" ", desc)).strip()
        if clean_desc.lower() == title.strip().lower():
            clean_desc = ""
        out.append({"title": title, "text": f"{title} {desc}", "ts": ts,
                    "link": a.get("url", ""), "summary": clean_desc, "source": source})
    return out


class NewsSignal:
    """Caches fetched articles for `refresh_minutes` so we don't hammer feeds every tick."""

    def __init__(self, cfg: dict, newsapi_key: str | None):
        self.feeds = cfg.get("feeds", [])
        self.refresh_seconds = cfg.get("refresh_minutes", 15) * 60
        self.lookback = timedelta(hours=cfg.get("lookback_hours", 12))
        self.max_articles = cfg.get("max_articles", 60)
        self.newsapi_key = newsapi_key
        # Configurable per instance -- a second NewsSignal for a non-crypto
        # symbol (e.g. aggressive_btc's SOXLUSDT) needs its own NewsAPI query,
        # not this default crypto one.
        self.newsapi_query = cfg.get("newsapi_query", "bitcoin OR ethereum OR crypto")
        self._cache_time = 0.0
        self._articles: list[dict] = []

    def _refresh_if_needed(self):
        now = time.time()
        if now - self._cache_time < self.refresh_seconds and self._articles:
            return
        articles = _fetch_rss(self.feeds, self.lookback)
        if self.newsapi_key:
            articles += _fetch_newsapi(self.newsapi_key, self.newsapi_query, self.lookback)
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

    def all_articles(self) -> list[dict]:
        """Every cached article for this pool (not filtered by symbol) --
        used by bot/newsfeed.py to collect raw article metadata (title/link/
        published time) for the dashboard's news-feed feature, which needs
        the full pool rather than one symbol's already-scored subset.
        Reuses whatever score_for_symbol's own refresh already fetched, so
        this never triggers an extra RSS fetch on its own.
        """
        self._refresh_if_needed()
        return list(self._articles)

    def score_for_symbol(self, symbol: str) -> dict:
        self._refresh_if_needed()
        keywords = _symbol_keywords(symbol)
        relevant = [a for a in self._articles if any(k in a["text"].lower() for k in keywords)]
        # No fallback to the general article pool when nothing symbol-specific
        # matched: for a universe-scanned altcoin, "general market mood" is
        # really just whatever's trending (usually BTC/ETH headlines) and has
        # no real bearing on that specific coin -- reporting it as if it were
        # relevant let unrelated macro news push a low-cap alt's direction.
        # Zero confidence here means zero influence on the aggregate (see
        # aggregator.aggregate), the same as any other signal with no data.
        if not relevant:
            return {"score": 0.0, "confidence": 0.0, "article_count": 0, "sample": [],
                    "bullish_count": 0, "bearish_count": 0, "neutral_count": 0}

        raw_scores = [_score_text(a["text"]) for a in relevant]
        avg = sum(raw_scores) / len(raw_scores)
        score = max(-1.0, min(1.0, avg / 2.5))
        confidence = max(0.0, min(1.0, len(relevant) / 15))

        bullish_count = sum(1 for s in raw_scores if s > 0)
        bearish_count = sum(1 for s in raw_scores if s < 0)
        neutral_count = sum(1 for s in raw_scores if s == 0)

        sample = [a["title"] for a in relevant[:5]]
        return {"score": score, "confidence": confidence, "article_count": len(relevant), "sample": sample,
                "bullish_count": bullish_count, "bearish_count": bearish_count, "neutral_count": neutral_count}
