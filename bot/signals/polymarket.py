"""Prediction-market signal, sourced from Polymarket's public Gamma API (read-only,
no API key required).

Polymarket markets don't map cleanly onto "will BTC go up in the next hour", so
this is treated as a *sentiment/risk* signal rather than a precise directional
one: it looks at relevant markets (crypto price targets, Fed policy, recession
odds, etc.), tags each with a bullish/bearish polarity from its question text,
and combines the current "Yes" probability level with how much that probability
has *moved* since the last refresh (momentum matters more than the static level
for a market that closes months out).

If Polymarket is unreachable or its response shape changes, this degrades to a
neutral, zero-confidence signal rather than crashing the bot -- it's one of four
inputs, not a dependency the bot can't live without.
"""
from __future__ import annotations

import json
import logging
import time

import requests

logger = logging.getLogger("bot.signals.polymarket")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

BULLISH_HINTS = [
    "all-time high", "ath", "reach $", "hit $", "surge", "rate cut", "adoption",
    "etf approval", "approved", "soft landing", "record high",
]
BEARISH_HINTS = [
    "recession", "rate hike", "crash", "drop below", "fall below", "ban",
    "hack", "default", "bear market", "below $", "hard landing",
]


def _polarity(question: str) -> int:
    q = question.lower()
    if any(h in q for h in BULLISH_HINTS):
        return 1
    if any(h in q for h in BEARISH_HINTS):
        return -1
    return 0


def _fetch_markets(limit: int = 200) -> list[dict]:
    try:
        resp = requests.get(
            GAMMA_MARKETS_URL,
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume", "ascending": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("polymarket fetch failed: %s", exc)
        return []
    return data if isinstance(data, list) else data.get("markets", [])


def _yes_price(market: dict) -> float | None:
    try:
        outcomes = market.get("outcomes")
        prices = market.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not outcomes or not prices:
            return None
        for name, price in zip(outcomes, prices):
            if str(name).strip().lower() == "yes":
                return float(price)
        return float(prices[0])
    except Exception:
        return None


class PolymarketSignal:
    def __init__(self, cfg: dict):
        self.keywords = [k.lower() for k in cfg.get("keywords", [])]
        self.refresh_seconds = cfg.get("refresh_minutes", 15) * 60
        self.max_markets = cfg.get("max_markets", 40)
        # Polymarket's markets here are broad crypto/macro questions (bitcoin,
        # Fed rate, recession...), not per-altcoin -- applying that as if it
        # were relevant to every scanned altcoin let unrelated macro sentiment
        # push a low-cap alt's direction. Only give real confidence for
        # symbols this signal is actually about; default to the majors its
        # own keywords name.
        self.relevant_symbols = set(cfg.get("relevant_symbols", ["BTCUSDT", "ETHUSDT"]))
        self._cache_time = 0.0
        self._relevant: list[dict] = []
        self._prev_prices: dict[str, float] = {}

    def _refresh_if_needed(self):
        now = time.time()
        if now - self._cache_time < self.refresh_seconds and self._relevant:
            return
        markets = _fetch_markets()
        relevant = []
        new_prev = {}
        for m in markets:
            question = (m.get("question") or "").strip()
            if not question:
                continue
            if not any(k in question.lower() for k in self.keywords):
                continue
            price = _yes_price(m)
            if price is None:
                continue
            market_id = str(m.get("conditionId") or m.get("id") or question)
            polarity = _polarity(question)
            volume = float(m.get("volume") or 0)
            prev_price = self._prev_prices.get(market_id)
            momentum = (price - prev_price) if prev_price is not None else 0.0
            relevant.append({
                "question": question, "price": price, "polarity": polarity,
                "volume": volume, "momentum": momentum,
            })
            new_prev[market_id] = price

        self._relevant = relevant[: self.max_markets]
        self._prev_prices = new_prev
        self._cache_time = now
        logger.info("polymarket refreshed: %d relevant markets", len(self._relevant))

    def score(self, symbol: str) -> dict:
        self._refresh_if_needed()
        if symbol not in self.relevant_symbols:
            return {"score": 0.0, "confidence": 0.0, "market_count": len(self._relevant), "sample": []}

        directional = [m for m in self._relevant if m["polarity"] != 0]
        if not directional:
            return {"score": 0.0, "confidence": 0.0, "market_count": len(self._relevant), "sample": []}

        import math
        total_weight = 0.0
        weighted_sum = 0.0
        for m in directional:
            weight = math.log10(max(m["volume"], 10))
            level_tilt = m["polarity"] * (m["price"] - 0.5) * 2  # -1..1
            momentum_tilt = m["polarity"] * m["momentum"] * 4    # amplified, small deltas expected
            contribution = 0.6 * level_tilt + 0.4 * max(-1.0, min(1.0, momentum_tilt))
            weighted_sum += contribution * weight
            total_weight += weight

        score = max(-1.0, min(1.0, weighted_sum / total_weight)) if total_weight else 0.0
        confidence = max(0.0, min(1.0, len(directional) / 8))
        sample = [f"{m['question']} ({m['price']:.2f})" for m in directional[:5]]
        return {"score": score, "confidence": confidence, "market_count": len(directional), "sample": sample}
