"""Screens Bybit's full linear-perpetual ticker list down to the most liquid
symbols by 24h turnover, so the bot can watch "whatever's actively trading"
instead of a hand-picked symbol list. One cheap bulk ticker call replaces what
would otherwise require a kline fetch per candidate symbol just to rank them.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("bot.signals.universe")


def screen_top_symbols(
    client,
    quote_suffix: str = "USDT",
    top_n: int = 30,
    max_taker_fee_rate: float | None = None,
    candidate_pool_mult: int = 2,
) -> list[str]:
    """Ranks by 24h turnover, optionally filtering out symbols whose taker fee
    rate exceeds `max_taker_fee_rate` (some newly-listed/lower-liquidity
    perpetuals charge double the standard rate -- a structural drag on every
    round trip regardless of how good the signal is). Filtering can only
    shrink the candidate list, so when it's on, turnover-rank a wider pool
    (top_n * candidate_pool_mult) first and backfill from it down to top_n,
    rather than silently returning fewer than top_n.
    """
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

    if max_taker_fee_rate is None:
        return [symbol for _, symbol in ranked[:top_n]]

    pool = ranked[: top_n * candidate_pool_mult]
    selected = []
    for _, symbol in pool:
        fee = client.get_taker_fee_rate(symbol)
        if fee is not None and fee > max_taker_fee_rate:
            logger.info("universe: skipping %s, taker fee %.4f%% exceeds max %.4f%%",
                        symbol, fee * 100, max_taker_fee_rate * 100)
            continue
        selected.append(symbol)
        if len(selected) >= top_n:
            break
    return selected
