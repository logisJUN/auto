from bot.signals import universe


class FakeClient:
    def __init__(self, tickers):
        self._tickers = tickers

    def get_all_tickers(self):
        return self._tickers


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
