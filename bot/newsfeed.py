"""Dashboard news-feed feature: collects every article/filing already being
fetched for aggressive_btc's symbols, scores each one's up/down lean with
the same free keyword lexicon news.py uses for its aggregated signal, and
tracks whether that lean (and the user's own prediction) matched what the
price actually did a few hours later. Purely observational -- nothing here
feeds back into trading decisions.

Needs a Postgres database (DATABASE_URL env var) to actually persist
anything. This project's bot/state.py-style local JSON files reset on every
Render redeploy (no persistent disk on the free plan -- see render_app.py's
module docstring), which is fine for live trading state (the exchange
itself is always the source of truth there, see
Strategy._reconcile_orphaned_positions) but defeats the entire point of
this feature: an accuracy track record that's wiped every time new code
ships is not a track record. A small always-on external Postgres (e.g. a
free Neon.tech or Supabase project) is the fix -- set DATABASE_URL to its
connection string in Render's environment variables. Every function here is
a no-op (ENABLED=False) until that's set, so deploying this code alone
changes nothing.

AI direction/confidence is deliberately the same zero-cost keyword lexicon
news.py already uses (not a real LLM call) -- see the ENTRY_THRESHOLD-style
request for an AI-vs-rule-based choice that led to this; a real LLM call
costs real money per request and this project has no API key provisioned
for one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone

from bot.exchange.bybit_client import BybitAPIError, BybitClient
from bot.signals.news import NewsSignal, score_text, symbol_keywords

logger = logging.getLogger("bot.newsfeed")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ENABLED = bool(DATABASE_URL)

# How long after an article is first seen we check whether the price moved
# the way its AI/user prediction expected. 4h balances "enough time for a
# real move to happen" against "don't make the user wait days to find out
# if a prediction was right." Bybit's tokenized-stock perpetuals trade
# continuously (same as crypto), so this window is never blocked by a
# closed exchange the way it would be for the real underlying stock.
RESOLUTION_WINDOW_SEC = 4 * 3600

KLINE_INTERVAL = "1"  # 1-minute candles, fine-grained enough for a "price at this moment" lookup

EARNINGS_KEYWORDS = [
    "earnings", "quarterly results", "quarterly report", "q1 20", "q2 20", "q3 20", "q4 20",
    "eps", "revenue", "guidance", "beats estimates", "misses estimates", "beat estimates",
    "fiscal year", "fiscal q", "net income", "profit warning",
]

_schema_ready = False


def _category_for(text: str) -> str:
    text = text.lower()
    return "earnings" if any(k in text for k in EARNINGS_KEYWORDS) else "news"


def _direction_from_score(score: int) -> str:
    if score > 0:
        return "up"
    if score < 0:
        return "down"
    return "neutral"


def _confidence_from_score(score: int) -> float:
    return max(0.0, min(1.0, abs(score) / 5.0))


def _article_id(symbol: str, link: str, title: str) -> str:
    key = link or title
    return hashlib.sha256(f"{symbol}|{key}".encode("utf-8")).hexdigest()[:32]


def _get_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS newsfeed_articles (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                link TEXT,
                source TEXT,
                category TEXT NOT NULL,
                published_at DOUBLE PRECISION NOT NULL,
                price_at_publish DOUBLE PRECISION,
                ai_direction TEXT NOT NULL,
                ai_confidence DOUBLE PRECISION NOT NULL,
                user_direction TEXT,
                user_predicted_at DOUBLE PRECISION,
                resolve_at DOUBLE PRECISION NOT NULL,
                resolved BOOLEAN NOT NULL DEFAULT FALSE,
                price_at_resolve DOUBLE PRECISION,
                actual_direction TEXT,
                ai_correct BOOLEAN,
                user_correct BOOLEAN,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_newsfeed_published_at "
                    "ON newsfeed_articles (published_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_newsfeed_resolve "
                    "ON newsfeed_articles (resolved, resolve_at)")
    conn.commit()
    _schema_ready = True


def _price_at(client: BybitClient, symbol: str, ts: float) -> float | None:
    try:
        candles = client.get_klines(symbol, KLINE_INTERVAL, limit=1, end_time_ms=int(ts * 1000))
    except BybitAPIError:
        return None
    if not candles:
        return None
    return candles[-1]["close"]


def save_article_if_new(symbol: str, article: dict, client: BybitClient, category: str | None = None) -> bool:
    """article is one of news.NewsSignal's cached article dicts (title/text/
    ts/link/summary/source). Returns True if a new row was inserted, False
    if it already existed (or ts is unparseable, or the feature is
    disabled) -- callers don't need to distinguish those, they just mean
    "nothing new to do here."
    """
    if not ENABLED:
        return False
    ts = article.get("ts")
    if ts is None:
        return False  # no anchor time -- can't compute price-at-publish or a resolve deadline
    link = article.get("link", "")
    title = article.get("title", "")
    article_id = _article_id(symbol, link, title)

    try:
        conn = _get_conn()
    except Exception:
        logger.warning("newsfeed: failed to connect to DATABASE_URL", exc_info=True)
        return False
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM newsfeed_articles WHERE id = %s", (article_id,))
            if cur.fetchone() is not None:
                return False

        published_at = ts.timestamp() if isinstance(ts, datetime) else float(ts)
        price_at_publish = _price_at(client, symbol, published_at)
        category = category or _category_for(article.get("text", title))
        # A bare SEC filing title carries no extractable sentiment (see
        # save_filing_article's docstring) -- scoring its words with the
        # same bullish/bearish lexicon used for real news would risk a false
        # read (e.g. a routine 8-K literally titled around "bankruptcy" or
        # "default" scoring as a strong bearish lean it never claimed to be).
        score = 0 if category == "filing" else score_text(article.get("text", title))

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO newsfeed_articles
                    (id, symbol, title, summary, link, source, category, published_at,
                     price_at_publish, ai_direction, ai_confidence, resolve_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (
                article_id, symbol, title, article.get("summary", ""), link, article.get("source", ""),
                category, published_at, price_at_publish, _direction_from_score(score),
                _confidence_from_score(score), published_at + RESOLUTION_WINDOW_SEC, time.time(),
            ))
        conn.commit()
        return True
    except Exception:
        logger.warning("newsfeed: failed to save article %s for %s", article_id, symbol, exc_info=True)
        return False
    finally:
        conn.close()


def collect_from_news_cache(news_cache: dict[int, NewsSignal], symbols_by_news_key: dict[int, list[str]],
                             client: BybitClient) -> None:
    """Called once per aggressive_btc main-loop iteration (see main()).
    Reuses the exact NewsSignal instances already fetching/caching RSS for
    trading signals -- no extra feed requests, just a wider read of what's
    already cached (all_articles(), not one symbol's score_for_symbol()
    subset), matched against each pool's own member symbols.
    """
    if not ENABLED:
        return
    for news_key, news_signal in news_cache.items():
        symbols = symbols_by_news_key.get(news_key, [])
        if not symbols:
            continue
        for article in news_signal.all_articles():
            text_lower = article.get("text", "").lower()
            for symbol in symbols:
                if any(k in text_lower for k in symbol_keywords(symbol)):
                    save_article_if_new(symbol, article, client)


def save_filing_article(symbol: str, filing: dict, client: BybitClient) -> bool:
    """filing is one of EdgarFilingWatcher.check_for_new_filings()'s dicts
    (ticker/title/link). Stored with ai_direction="neutral"/confidence=0 --
    see bot/signals/edgar.py's module docstring on why a bare 8-K filing
    title carries no extractable sentiment; the user can still predict
    up/down on it, it just never counts toward the AI's own accuracy stats
    (ai_correct stays NULL for a neutral row -- see get_accuracy_stats).
    published_at is "now" (detection time), not the filing's own timestamp --
    the current-events feed only ever shows very recent filings anyway, so
    this is a close enough anchor for the price-at-publish lookup.
    """
    if not ENABLED:
        return False
    article = {"title": filing["title"], "text": filing["title"], "ts": datetime.now(timezone.utc),
               "link": filing.get("link", ""), "summary": "", "source": "SEC EDGAR"}
    return save_article_if_new(symbol, article, client, category="filing")


def set_user_prediction(article_id: str, direction: str) -> bool:
    """One-shot: a user can only set their own prediction once per article,
    and only before it's resolved -- otherwise "predicting" after the
    outcome is already knowable would make the accuracy track record
    meaningless.
    """
    if not ENABLED or direction not in ("up", "down"):
        return False
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("newsfeed: failed to connect to DATABASE_URL", exc_info=True)
        return False
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE newsfeed_articles SET user_direction = %s, user_predicted_at = %s
                WHERE id = %s AND user_direction IS NULL AND resolved = FALSE
            """, (direction, time.time(), article_id))
            updated = cur.rowcount > 0
        conn.commit()
        return updated
    except Exception:
        logger.warning("newsfeed: failed to save user prediction for %s", article_id, exc_info=True)
        return False
    finally:
        conn.close()


def resolve_due_articles(client: BybitClient, limit: int = 20) -> int:
    """Called once per aggressive_btc main-loop iteration. Finds articles
    whose resolution deadline has passed and weren't resolved yet, fetches
    the price at that deadline, and fills in whether the AI's/user's
    prediction was actually right. `limit` bounds how many Bybit calls one
    loop iteration can make even if many articles become due at once.
    """
    if not ENABLED:
        return 0
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("newsfeed: failed to connect to DATABASE_URL", exc_info=True)
        return 0
    resolved_count = 0
    try:
        _ensure_schema(conn)
        now = time.time()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, price_at_publish, ai_direction, user_direction, resolve_at
                FROM newsfeed_articles WHERE resolved = FALSE AND resolve_at <= %s
                ORDER BY resolve_at ASC LIMIT %s
            """, (now, limit))
            rows = cur.fetchall()

        for article_id, symbol, price_before, ai_direction, user_direction, resolve_at in rows:
            price_after = _price_at(client, symbol, resolve_at)
            if price_after is None:
                continue  # try again next cycle rather than resolving with missing data
            if price_before is None:
                actual_direction, ai_correct, user_correct = None, None, None
            else:
                actual_direction = "up" if price_after > price_before else ("down" if price_after < price_before else "neutral")
                ai_correct = (ai_direction == actual_direction) if ai_direction != "neutral" else None
                user_correct = (user_direction == actual_direction) if user_direction else None
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE newsfeed_articles
                    SET resolved = TRUE, price_at_resolve = %s, actual_direction = %s,
                        ai_correct = %s, user_correct = %s
                    WHERE id = %s
                """, (price_after, actual_direction, ai_correct, user_correct, article_id))
            resolved_count += 1
        conn.commit()
    except Exception:
        logger.warning("newsfeed: failed to resolve due articles", exc_info=True)
    finally:
        conn.close()
    return resolved_count


_COLUMNS = ["id", "symbol", "title", "summary", "link", "source", "category", "published_at",
            "price_at_publish", "ai_direction", "ai_confidence", "user_direction", "user_predicted_at",
            "resolve_at", "resolved", "price_at_resolve", "actual_direction", "ai_correct", "user_correct",
            "created_at"]


def _row_to_dict(row) -> dict:
    return dict(zip(_COLUMNS, row))


def list_recent_articles(limit: int = 50, symbol: str | None = None) -> list[dict]:
    if not ENABLED:
        return []
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("newsfeed: failed to connect to DATABASE_URL", exc_info=True)
        return []
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if symbol:
                cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM newsfeed_articles "
                            f"WHERE symbol = %s ORDER BY published_at DESC LIMIT %s", (symbol, limit))
            else:
                cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM newsfeed_articles "
                            f"ORDER BY published_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.warning("newsfeed: failed to list articles", exc_info=True)
        return []
    finally:
        conn.close()


def get_article(article_id: str) -> dict | None:
    if not ENABLED:
        return None
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("newsfeed: failed to connect to DATABASE_URL", exc_info=True)
        return None
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM newsfeed_articles WHERE id = %s", (article_id,))
            row = cur.fetchone()
        return _row_to_dict(row) if row else None
    except Exception:
        logger.warning("newsfeed: failed to fetch article %s", article_id, exc_info=True)
        return None
    finally:
        conn.close()


def get_accuracy_stats() -> dict:
    """Overall AI/user accuracy plus a per-category breakdown of how the AI's
    lean actually played out (e.g. category='earnings', ai_direction='up',
    actual_direction='down' -- "흑자/호재 신호였는데 실제로는 하락"), which is
    the accumulated judgment-vs-outcome data the dashboard surfaces.
    """
    empty = {"ai_judged": 0, "ai_correct": 0, "ai_accuracy_pct": None,
             "user_judged": 0, "user_correct": 0, "user_accuracy_pct": None, "breakdown": []}
    if not ENABLED:
        return empty
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("newsfeed: failed to connect to DATABASE_URL", exc_info=True)
        return empty
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE ai_correct IS NOT NULL) AS ai_judged,
                    COUNT(*) FILTER (WHERE ai_correct IS TRUE) AS ai_correct,
                    COUNT(*) FILTER (WHERE user_correct IS NOT NULL) AS user_judged,
                    COUNT(*) FILTER (WHERE user_correct IS TRUE) AS user_correct
                FROM newsfeed_articles
            """)
            ai_judged, ai_correct, user_judged, user_correct = cur.fetchone()

            cur.execute("""
                SELECT category, ai_direction, actual_direction, COUNT(*)
                FROM newsfeed_articles
                WHERE resolved = TRUE AND ai_direction != 'neutral' AND actual_direction IS NOT NULL
                GROUP BY category, ai_direction, actual_direction
                ORDER BY category, ai_direction, actual_direction
            """)
            breakdown = [
                {"category": c, "ai_direction": a, "actual_direction": act, "count": n}
                for c, a, act, n in cur.fetchall()
            ]
        return {
            "ai_judged": ai_judged, "ai_correct": ai_correct,
            "ai_accuracy_pct": (ai_correct / ai_judged * 100.0) if ai_judged else None,
            "user_judged": user_judged, "user_correct": user_correct,
            "user_accuracy_pct": (user_correct / user_judged * 100.0) if user_judged else None,
            "breakdown": breakdown,
        }
    except Exception:
        logger.warning("newsfeed: failed to compute accuracy stats", exc_info=True)
        return empty
    finally:
        conn.close()
