"""bot/backtest.py: replays aggressive_btc's technical+volume signal (news/
polymarket forced to zero -- see module docstring for why) against candle
data to validate the entry/TP/SL/hourly-cutoff logic independent of live
trading. These tests use synthetic candles and monkeypatched technical
scores (no network) -- fetch_historical_klines (the only part that needs
real Bybit data) is tested separately with a mocked client.
"""
from unittest.mock import MagicMock

import bot.backtest as backtest
from bot.backtest import fetch_historical_klines, simulate
from bot.exchange.bybit_client import BybitClient


def _candle(ts, o, h, l, c, v=1000.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat_candles(n, price=100.0, start_ts=0):
    """n flat candles at `price`, far enough from TP/SL to never trigger
    them on their own -- used as padding/warmup.
    """
    return [_candle(start_ts + i * 3600_000, price, price, price, price) for i in range(n)]


# -- simulate() ---------------------------------------------------------

def test_simulate_enters_long_and_exits_on_tp_hit(monkeypatch):
    # Strong, constant bullish score -> enters long at the first eligible candle.
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.9, "atr": 1.0, "close": 100.0, "rsi": 70.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(61, price=100.0)  # warmup=60 -> entry at index 60, close=100
    # atr_pct = 1.0/100*100 = 1.0 -> leverage = min(10, max(6, min(10, 12/1.0))) = 10
    # target move = 12%/10 = 1.2% -> TP = 101.2; stop move = 5%/10 = 0.5% -> SL = 99.5
    candles.append(_candle(61 * 3600_000, 100.0, 101.5, 99.9, 101.0))  # high clears TP

    result = simulate(candles, warmup=60)

    assert result["trade_count"] == 1
    trade = result["trades"][0]
    assert trade.side == "long"
    assert trade.reason == "tp"
    assert trade.entry_price == 100.0
    assert round(trade.fee_pct, 2) == 1.1  # 0.11% round-trip fee * 10x leverage
    assert round(trade.roi_pct, 2) == 10.9  # +12% gross TP hit, minus the 1.1% fee
    assert result["win_rate"] == 1.0
    assert round(result["total_fee_pct"], 2) == 1.1


def test_simulate_exits_on_sl_hit(monkeypatch):
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.9, "atr": 1.0, "close": 100.0, "rsi": 70.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(61, price=100.0)
    candles.append(_candle(61 * 3600_000, 100.0, 100.2, 99.0, 99.5))  # low breaches SL (99.5), TP untouched

    result = simulate(candles, warmup=60)

    assert result["trade_count"] == 1
    trade = result["trades"][0]
    assert trade.reason == "sl"
    assert round(trade.roi_pct, 2) == -6.1  # -5% gross SL hit, minus the 1.1% fee
    assert result["win_rate"] == 0.0


def test_simulate_fee_rate_is_configurable_and_scales_with_leverage(monkeypatch):
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.9, "atr": 1.0, "close": 100.0, "rsi": 70.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(61, price=100.0)
    candles.append(_candle(61 * 3600_000, 100.0, 101.5, 99.9, 101.0))  # TP hit, leverage=10x

    zero_fee = simulate(candles, warmup=60, round_trip_fee_pct_of_notional=0.0)
    assert round(zero_fee["trades"][0].roi_pct, 2) == 12.0  # gross, unaffected
    assert zero_fee["total_fee_pct"] == 0.0

    custom_fee = simulate(candles, warmup=60, round_trip_fee_pct_of_notional=0.2)
    assert round(custom_fee["trades"][0].fee_pct, 2) == 2.0  # 0.2% * 10x leverage
    assert round(custom_fee["trades"][0].roi_pct, 2) == 10.0  # 12% - 2%


def test_simulate_assumes_the_worse_outcome_when_tp_and_sl_both_touched_in_one_candle(monkeypatch):
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.9, "atr": 1.0, "close": 100.0, "rsi": 70.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(61, price=100.0)
    # wide candle: high clears TP (101.2) AND low breaches SL (99.5) in the same candle
    candles.append(_candle(61 * 3600_000, 100.0, 102.0, 99.0, 100.5))

    result = simulate(candles, warmup=60)

    assert result["trades"][0].reason == "sl"  # conservative: SL wins the ambiguity


def test_simulate_never_enters_when_signal_stays_neutral(monkeypatch):
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.05, "atr": 1.0, "close": 100.0, "rsi": 51.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(80, price=100.0)

    result = simulate(candles, warmup=60)

    assert result["trade_count"] == 0
    assert result["win_rate"] is None
    assert result["trades"] == []


def test_simulate_closes_on_hourly_cutoff_when_roi_still_negative(monkeypatch):
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.9, "atr": 1.0, "close": 100.0, "rsi": 70.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(61, price=100.0)
    # price drifts slightly negative but never actually reaches SL (99.5) or TP (101.2)
    # in the very next candle -> hourly cutoff should close it once hours_open >= 1 and ROI <= 0.
    # (the signal stays constantly bullish here, so it re-enters immediately after each
    # cutoff close -- no cooldown, same as the live bot -- hence >= 1 rather than == 1.)
    for k in range(1, 5):
        candles.append(_candle((61 + k) * 3600_000, 99.9, 99.95, 99.8, 99.9))

    result = simulate(candles, warmup=60)

    assert result["trade_count"] >= 1
    assert result["trades"][0].reason == "hourly_cutoff"
    assert all(t.reason == "hourly_cutoff" for t in result["trades"])


def test_simulate_stops_without_fabricating_an_exit_when_data_runs_out_mid_trade(monkeypatch):
    monkeypatch.setattr(backtest.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.9, "atr": 1.0, "close": 100.0, "rsi": 70.0})
    monkeypatch.setattr(backtest.technical, "volume_score", lambda candles: 0.0)

    candles = _flat_candles(61, price=100.0)  # enters at index 60, then data just ends

    result = simulate(candles, warmup=60)

    assert result["trade_count"] == 0  # the one entry never got a recorded exit


# -- fetch_historical_klines ---------------------------------------------------------

def test_fetch_historical_klines_pages_backward_and_stitches_oldest_to_newest():
    client = MagicMock(spec=BybitClient)
    # First call (no end_time_ms): most recent page_size candles (ts 1000..1999, oldest->newest)
    page_recent = [_candle(ts, 1, 1, 1, 1) for ts in range(1000, 2000)]
    # Second call (end_time_ms=999): the page right before it (ts 0..999)
    page_older = [_candle(ts, 1, 1, 1, 1) for ts in range(0, 1000)]

    def fake_get_klines(symbol, interval, limit, end_time_ms=None):
        if end_time_ms is None:
            return page_recent
        assert end_time_ms == 999  # == page_recent's oldest ts - 1
        return page_older

    client.get_klines.side_effect = fake_get_klines

    result = fetch_historical_klines(client, "SOXLUSDT", "60", total_candles=1500, page_size=1000)

    assert len(result) == 1500
    assert [c["ts"] for c in result] == sorted(c["ts"] for c in result)  # oldest->newest
    assert result[0]["ts"] == 500  # trimmed to the most recent 1500 of the 2000 fetched
    assert result[-1]["ts"] == 1999


def test_fetch_historical_klines_stops_when_exchange_runs_out_of_history():
    client = MagicMock(spec=BybitClient)
    page1 = [_candle(ts, 1, 1, 1, 1) for ts in range(100, 200)]  # 100 candles, short of page_size
    client.get_klines.return_value = page1

    result = fetch_historical_klines(client, "SOXLUSDT", "60", total_candles=1000, page_size=1000)

    client.get_klines.assert_called_once()  # didn't keep paginating past a short/empty batch
    assert len(result) == 100
