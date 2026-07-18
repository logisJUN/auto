"""Screens Bybit's full linear-perpetual ticker list down to the most liquid
symbols by 24h turnover, so the bot can watch "whatever's actively trading"
instead of a hand-picked symbol list. One cheap bulk ticker call replaces what
would otherwise require a kline fetch per candidate symbol just to rank them.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("bot.signals.universe")


def screen_top_symbols(client, quote_suffix: str = "USDT", top_n: int = 30) -> list[str]:
    tickers = client.get_all_tickers()

    ranked = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith(quote_suffix):
            continue
        raw_turnover = t.get("turnover24h")
        if raw_turnover is None:
            continue
        try:
            turnover = float(raw_turnover)
        except (TypeError, ValueError):
            continue
        ranked.append((turnover, symbol))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [symbol for _, symbol in ranked[:top_n]]
