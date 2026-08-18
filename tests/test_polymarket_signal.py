"""PolymarketSignal.score(symbol): these markets are broad crypto/macro
questions (bitcoin, Fed rate, recession...), not per-altcoin. Applying that as
if it were relevant to every scanned altcoin let unrelated macro sentiment
push a low-cap alt's direction, so it's now only given real confidence for
symbols it's actually about (relevant_symbols, default BTC/ETH) -- zero
confidence for everything else, same as any other signal with no real data.
"""
import time

from bot.signals.polymarket import PolymarketSignal


def _primed(directional_markets: list[dict], relevant_symbols=None) -> PolymarketSignal:
    cfg = {"keywords": ["bitcoin"]}
    if relevant_symbols is not None:
        cfg["relevant_symbols"] = relevant_symbols
    signal = PolymarketSignal(cfg)
    signal._relevant = directional_markets
    signal._cache_time = time.time()  # cache looks fresh -- skips the network fetch
    return signal


_BULLISH_MARKET = {"question": "Will bitcoin reach $100k?", "price": 0.7, "polarity": 1,
                    "volume": 1_000_000.0, "momentum": 0.0}


def test_irrelevant_symbol_gets_zero_confidence_regardless_of_market_data():
    signal = _primed([_BULLISH_MARKET])

    result = signal.score("SOMEALTUSDT")

    assert result["score"] == 0.0
    assert result["confidence"] == 0.0


def test_relevant_symbol_gets_the_real_computed_score():
    signal = _primed([_BULLISH_MARKET])

    result = signal.score("BTCUSDT")

    assert result["score"] > 0
    assert result["confidence"] > 0


def test_relevant_symbols_list_is_configurable():
    signal = _primed([_BULLISH_MARKET], relevant_symbols=["SOMEALTUSDT"])

    assert signal.score("SOMEALTUSDT")["confidence"] > 0
    assert signal.score("BTCUSDT")["confidence"] == 0.0  # no longer in the configured list
