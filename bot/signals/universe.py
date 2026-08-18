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
    min_max_leverage: float | None = None,
    candidate_pool_mult: int = 2,
    excluded_symbols: set[str] | None = None,
) -> list[str]:
    """Ranks by 24h turnover, optionally filtering out symbols whose taker fee
    rate exceeds `max_taker_fee_rate` (some newly-listed/lower-liquidity
    perpetuals charge double the standard rate -- a structural drag on every
    round trip regardless of how good the signal is). Filtering can only
    shrink the candidate list, so when it's on, turnover-rank a wider pool
    (top_n * candidate_pool_mult) first and backfill from it down to top_n,
    rather than silently returning fewer than top_n.

    `excluded_symbols` drops specific symbols entirely (backfilled just like a
    fee-filtered one) -- meant for tokenized-stock/commodity perpetuals that
    ride Bybit's linear-USDT list alongside real crypto (e.g. SOXLUSDT is a
    3x-leveraged semiconductor ETF token, not a coin) but trade on entirely
    different drivers than the technical/news/funding signals here assume,
    and can carry account/region restrictions a real crypto pair wouldn't.

    `min_max_leverage` is a systematic backstop for the same problem instead of
    reacting one symbol at a time as each new lookalike is found live (three
    separate tickers caught this way so far, including the same underlying
    reappearing under a second ticker after the first was already excluded).
    Real crypto perpetuals on Bybit typically offer much higher max leverage
    than these CFD-style stock/commodity-linked contracts, which are usually
    capped much lower -- so a symbol whose own exchange-reported max leverage
    falls below this floor is skipped just like a fee-filtered one. Not a
    replacement for `excluded_symbols` (a known bad symbol is still skipped
    outright and cheaply, without needing an instrument-info call), just a
    second layer that also catches ones not yet added there.
    """
    excluded_symbols = excluded_symbols or set()
    tickers = client.get_all_tickers()

    ranked = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith(quote_suffix):
            continue
        if symbol in excluded_symbols:
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

    if max_taker_fee_rate is None and min_max_leverage is None:
        return [symbol for _, symbol in ranked[:top_n]]

    pool = ranked[: top_n * candidate_pool_mult]
    selected = []
    for _, symbol in pool:
        if max_taker_fee_rate is not None:
            fee = client.get_taker_fee_rate(symbol)
            if fee is not None and fee > max_taker_fee_rate:
                logger.info("universe: skipping %s, taker fee %.4f%% exceeds max %.4f%%",
                            symbol, fee * 100, max_taker_fee_rate * 100)
                continue
        if min_max_leverage is not None:
            max_lev = client.get_instrument_info(symbol).max_leverage
            if max_lev is not None and max_lev < min_max_leverage:
                logger.info("universe: skipping %s, max leverage %.1fx below min %.1fx "
                            "(likely a non-crypto/CFD-style listing)",
                            symbol, max_lev, min_max_leverage)
                continue
        selected.append(symbol)
        if len(selected) >= top_n:
            break
    return selected
