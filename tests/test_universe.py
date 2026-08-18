from bot.signals import universe


class FakeInstrumentInfo:
    def __init__(self, max_leverage):
        self.max_leverage = max_leverage


class FakeClient:
    def __init__(self, tickers, fee_rates=None, max_leverages=None):
        self._tickers = tickers
        self._fee_rates = fee_rates or {}
        self._max_leverages = max_leverages or {}

    def get_all_tickers(self):
        return self._tickers

    def get_taker_fee_rate(self, symbol):
        return self._fee_rates.get(symbol)

    def get_instrument_info(self, symbol):
        return FakeInstrumentInfo(self._max_leverages.get(symbol, 25.0))


def test_ranks_by_turnover_and_filters_quote_suffix():
    tickers = [
        {"symbol": "BTCUSDT", "turnover24h": "500000000"},
        {"symbol": "ETHUSDT", "turnover24h": "300000000"},
        {"symbol": "OBSCUREUSDT", "turnover24h": "1000"},
        {"symbol": "BTCUSDC", "turnover24h": "999999999"},  # wrong quote, must be excluded
    ]
    client = FakeClient(tickers)
    result = universe.screen_top_symbols(client, quote_suffix="USDT", top_n=2)
    assert result == ["BTCUSDT", "ETHUSDT"]


def test_top_n_caps_result_length():
    tickers = [{"symbol": f"COIN{i}USDT", "turnover24h": str(i)} for i in range(10)]
    client = FakeClient(tickers)
    result = universe.screen_top_symbols(client, top_n=3)
    assert len(result) == 3
    # highest turnover (COIN9) first
    assert result[0] == "COIN9USDT"


def test_handles_missing_or_malformed_turnover_gracefully():
    tickers = [
        {"symbol": "AUSDT", "turnover24h": "100"},
        {"symbol": "BUSDT"},  # missing turnover24h
        {"symbol": "CUSDT", "turnover24h": "not-a-number"},
    ]
    client = FakeClient(tickers)
    result = universe.screen_top_symbols(client, top_n=10)
    assert result == ["AUSDT"]


def test_fee_filter_skips_high_fee_symbols_and_backfills():
    # ranked by turnover: HIGHFEE (best) > OK1 > OK2 > OK3 -- HIGHFEE should be
    # dropped for its fee rate and backfilled by the next-best candidate.
    tickers = [
        {"symbol": "HIGHFEEUSDT", "turnover24h": "1000"},
        {"symbol": "OK1USDT", "turnover24h": "900"},
        {"symbol": "OK2USDT", "turnover24h": "800"},
        {"symbol": "OK3USDT", "turnover24h": "700"},
    ]
    fee_rates = {
        "HIGHFEEUSDT": 0.0011,  # double the standard rate -- excluded
        "OK1USDT": 0.00055,
        "OK2USDT": 0.00055,
        "OK3USDT": 0.00055,
    }
    client = FakeClient(tickers, fee_rates)
    result = universe.screen_top_symbols(client, top_n=3, max_taker_fee_rate=0.0006, candidate_pool_mult=4)
    assert result == ["OK1USDT", "OK2USDT", "OK3USDT"]
    assert "HIGHFEEUSDT" not in result


def test_fee_filter_disabled_by_default_keeps_high_fee_symbol():
    tickers = [{"symbol": "HIGHFEEUSDT", "turnover24h": "1000"}]
    client = FakeClient(tickers, {"HIGHFEEUSDT": 0.0011})
    result = universe.screen_top_symbols(client, top_n=1)  # max_taker_fee_rate=None (default)
    assert result == ["HIGHFEEUSDT"]


def test_fee_filter_treats_unknown_fee_rate_as_acceptable():
    # if the fee-rate lookup fails (returns None), don't punish the symbol for it.
    tickers = [{"symbol": "UNKNOWNUSDT", "turnover24h": "1000"}]
    client = FakeClient(tickers, fee_rates={})  # get_taker_fee_rate returns None
    result = universe.screen_top_symbols(client, top_n=1, max_taker_fee_rate=0.0006)
    assert result == ["UNKNOWNUSDT"]


def test_excluded_symbols_are_dropped_and_backfilled():
    tickers = [
        {"symbol": "SOXLUSDT", "turnover24h": "1000"},  # highest turnover -- excluded
        {"symbol": "AUSDT", "turnover24h": "900"},
        {"symbol": "BUSDT", "turnover24h": "800"},
    ]
    client = FakeClient(tickers)
    result = universe.screen_top_symbols(client, top_n=2, excluded_symbols={"SOXLUSDT"})
    assert result == ["AUSDT", "BUSDT"]
    assert "SOXLUSDT" not in result


def test_excluded_symbols_empty_by_default():
    tickers = [{"symbol": "SOXLUSDT", "turnover24h": "1000"}]
    client = FakeClient(tickers)
    result = universe.screen_top_symbols(client, top_n=1)
    assert result == ["SOXLUSDT"]


def test_leverage_filter_skips_low_leverage_symbols_and_backfills():
    # ranked by turnover: LOWLEV (best) > OK1 > OK2 -- LOWLEV should be dropped
    # for its low max leverage (like a CFD-style stock/commodity token) and
    # backfilled by the next-best real-crypto-leverage candidate.
    tickers = [
        {"symbol": "LOWLEVUSDT", "turnover24h": "1000"},
        {"symbol": "OK1USDT", "turnover24h": "900"},
        {"symbol": "OK2USDT", "turnover24h": "800"},
    ]
    max_leverages = {"LOWLEVUSDT": 10.0, "OK1USDT": 25.0, "OK2USDT": 25.0}
    client = FakeClient(tickers, max_leverages=max_leverages)
    result = universe.screen_top_symbols(client, top_n=2, min_max_leverage=12.5, candidate_pool_mult=4)
    assert result == ["OK1USDT", "OK2USDT"]
    assert "LOWLEVUSDT" not in result


def test_leverage_filter_disabled_by_default_keeps_low_leverage_symbol():
    tickers = [{"symbol": "LOWLEVUSDT", "turnover24h": "1000"}]
    client = FakeClient(tickers, max_leverages={"LOWLEVUSDT": 10.0})
    result = universe.screen_top_symbols(client, top_n=1)  # min_max_leverage=None (default)
    assert result == ["LOWLEVUSDT"]


def test_leverage_at_or_above_the_floor_is_not_filtered():
    tickers = [{"symbol": "OKUSDT", "turnover24h": "1000"}]
    client = FakeClient(tickers, max_leverages={"OKUSDT": 12.5})
    result = universe.screen_top_symbols(client, top_n=1, min_max_leverage=12.5)
    assert result == ["OKUSDT"]
