import pytest

from bot.risk import stress_test


def _make_candles(n=30, high=101.0, low=99.0, close=100.0):
    return [
        {"ts": 1_700_000_000 + i * 900_000, "open": close, "high": high, "low": low,
         "close": close, "volume": 100.0}
        for i in range(n)
    ]


class FakeConfig:
    """Minimal stand-in for bot.config.Config's .get(*path, default=...) API."""

    def __init__(self, raw):
        self.raw = raw

    def get(self, *path, default=None):
        node = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


class FakeClient:
    def __init__(self, candles):
        self._candles = candles

    def get_klines(self, symbol, interval, limit):
        return self._candles


RAW_CFG = {
    "exchange": {"symbols": ["AAAUSDT", "BBBUSDT"]},
    "risk": {
        "position_size_pct_of_equity": 25.0,
        "max_leverage": 5,
        "max_daily_loss_pct": 8.0,
        "leverage_by_symbol": {
            "AAAUSDT": {"min": 5, "max": 10},
            "BBBUSDT": {"min": 3, "max": 5},
        },
    },
    "trade_management": {"atr_sl_multiplier": 1.5},
    "signals": {"technical": {"timeframes": ["15", "60", "240"], "atr_period": 14, "kline_limit": 200}},
}


def test_worst_case_matches_hand_calculation():
    # constant high=101/low=99/close=100 candles -> ATR converges to exactly 2.0
    client = FakeClient(_make_candles())
    cfg = FakeConfig(RAW_CFG)
    result = stress_test.compute_worst_case(client, cfg, equity=1000.0)

    by_symbol = {p["symbol"]: p for p in result["per_symbol"]}
    # sl_distance_pct = atr(2.0) * atr_sl_multiplier(1.5) / close(100) * 100 = 3.0%
    assert by_symbol["AAAUSDT"]["sl_distance_pct"] == 3.0
    # loss_pct_of_equity = margin_pct(0.25) * leverage(10) * sl_distance_pct(3.0)
    assert by_symbol["AAAUSDT"]["loss_pct_of_equity"] == 7.5
    assert by_symbol["BBBUSDT"]["loss_pct_of_equity"] == 3.75

    assert result["worst_case_total_loss_pct"] == 11.25
    assert result["worst_case_total_loss_usdt"] == 112.5
    assert result["exceeds_daily_loss_limit"] is True


def test_worst_case_under_limit_when_leverage_low():
    cfg_raw = {**RAW_CFG, "risk": {**RAW_CFG["risk"], "leverage_by_symbol": {
        "AAAUSDT": {"min": 1, "max": 2}, "BBBUSDT": {"min": 1, "max": 2},
    }}}
    client = FakeClient(_make_candles())
    result = stress_test.compute_worst_case(client, FakeConfig(cfg_raw), equity=1000.0)
    # 0.25 * 2 * 3.0 = 1.5% per symbol, 3.0% total -- well under max_daily_loss_pct(8)
    assert result["worst_case_total_loss_pct"] == 3.0
    assert result["exceeds_daily_loss_limit"] is False


def test_worst_case_handles_fetch_error_gracefully():
    class BrokenClient:
        def get_klines(self, symbol, interval, limit):
            raise RuntimeError("network down")

    result = stress_test.compute_worst_case(BrokenClient(), FakeConfig(RAW_CFG), equity=1000.0)
    assert all("error" in p for p in result["per_symbol"])
    assert result["worst_case_total_loss_pct"] == 0.0


def test_open_trade_uses_real_margin_not_hypothetical_target():
    """A currently-open position must be sized off what it actually committed
    (qty*entry/leverage), not the target margin_pct% every symbol would get if
    opened in isolation -- margin_buffer_pct already shrinks real allocations
    for later positions, and the stress test must reflect that, not overstate it.
    """
    client = FakeClient(_make_candles())
    open_trades = {
        "AAAUSDT": {"qty": 1.0, "entry_price": 100.0, "leverage": 10, "risk_distance": 3.0},
    }
    result = stress_test.compute_worst_case(client, FakeConfig(RAW_CFG), equity=1000.0,
                                             symbols=["AAAUSDT"], open_trades=open_trades)

    by_symbol = {p["symbol"]: p for p in result["per_symbol"]}
    # real margin = 1.0*100/10 = 10 -> 1% of equity(1000); sl_distance = 3.0/100*100 = 3.0%
    # loss_pct_of_equity = 0.01 * 10 * 3.0 = 0.3 (vs. the hypothetical 25%*10*3.0=7.5)
    assert by_symbol["AAAUSDT"]["loss_pct_of_equity"] == pytest.approx(0.3)
    assert result["worst_case_total_loss_pct"] == pytest.approx(0.3)


def test_open_trade_and_hypothetical_symbol_combine_correctly():
    client = FakeClient(_make_candles())
    open_trades = {"AAAUSDT": {"qty": 1.0, "entry_price": 100.0, "leverage": 10, "risk_distance": 3.0}}
    result = stress_test.compute_worst_case(client, FakeConfig(RAW_CFG), equity=1000.0,
                                             symbols=["AAAUSDT", "BBBUSDT"], open_trades=open_trades)

    by_symbol = {p["symbol"]: p for p in result["per_symbol"]}
    assert by_symbol["AAAUSDT"]["loss_pct_of_equity"] == pytest.approx(0.3)  # real, from open_trades
    assert by_symbol["BBBUSDT"]["loss_pct_of_equity"] == 3.75  # hypothetical, as before
    assert result["worst_case_total_loss_pct"] == pytest.approx(0.3 + 3.75)
