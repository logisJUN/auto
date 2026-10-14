"""bot/aggressive_btc.py: a standalone, SEPARATE single-symbol aggressive
strategy (currently SOXLUSDT; 12% target / 1h, -5% SL, hourly negative-ROI
cutoff, 6-10x leverage hard-capped at 10x, 50% margin per trade). Deliberately
isolated from the main bot/strategy.py -- these tests cover its own pure
logic and its own strategy class in isolation.
"""
import time
from unittest.mock import MagicMock

import bot.aggressive_btc as aggressive_btc
from bot.aggressive_btc import (
    SYMBOL,
    AggressiveBTCStrategy,
    Secrets,
    _resolve_credentials,
    compute_leverage,
    compute_signal,
    compute_tp_sl_prices,
    roi_pct,
    should_hourly_cutoff,
)
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.state import StateStore


# -- compute_leverage ---------------------------------------------------------

def test_leverage_scales_up_when_recent_volatility_is_low():
    # low atr_pct -> required (12/atr) leverage would be huge -> clipped to max 10
    assert compute_leverage(atr_pct=0.1) == 10.0


def test_leverage_scales_down_when_recent_volatility_is_high():
    # high atr_pct -> required leverage would be tiny -> clipped up to min 6
    assert compute_leverage(atr_pct=5.0) == 6.0


def test_leverage_exact_value_in_the_middle_of_the_range():
    # required = 12/1.5 = 8, within [6,10] -- used as-is
    assert compute_leverage(atr_pct=1.5) == 8.0


def test_leverage_defaults_to_min_when_atr_is_zero_or_negative():
    assert compute_leverage(atr_pct=0.0) == 6.0
    assert compute_leverage(atr_pct=-1.0) == 6.0


def test_leverage_hard_cap_binds_even_if_the_normal_range_is_widened():
    # if max_leverage were raised above hard_cap, hard_cap still wins
    assert compute_leverage(atr_pct=0.01, max_leverage=100.0, hard_cap=25.0) == 25.0


# -- compute_tp_sl_prices ---------------------------------------------------------

def test_tp_sl_prices_for_a_long():
    prices = compute_tp_sl_prices(entry_price=100.0, side="long", leverage=12.0)
    # target move = 12%/12 = 1% -> TP = 101; stop move = 5%/12 -> SL = 100*(1-0.05/12)
    assert prices["tp"] == 101.0
    assert round(prices["sl"], 4) == round(100.0 * (1 - 0.05 / 12), 4)
    assert prices["sl"] < 100.0 < prices["tp"]


def test_tp_sl_prices_for_a_short():
    prices = compute_tp_sl_prices(entry_price=100.0, side="short", leverage=12.0)
    assert prices["tp"] == 99.0
    assert prices["tp"] < 100.0 < prices["sl"]


def test_tp_sl_prices_tighten_with_higher_leverage():
    low_lev = compute_tp_sl_prices(entry_price=100.0, side="long", leverage=6.0)
    high_lev = compute_tp_sl_prices(entry_price=100.0, side="long", leverage=24.0)
    # same % ROI target needs a smaller price move at higher leverage
    assert (high_lev["tp"] - 100.0) < (low_lev["tp"] - 100.0)
    assert (100.0 - high_lev["sl"]) < (100.0 - low_lev["sl"])


# -- compute_signal ---------------------------------------------------------

def test_signal_long_when_blend_strongly_bullish():
    tech = {"score": 0.9, "atr": 1.0, "close": 100.0}
    poly = {"score": 0.8, "confidence": 0.8}
    result = compute_signal(tech, volume_score=0.5, poly_result=poly)
    assert result["direction"] == "long"


def test_signal_short_when_blend_strongly_bearish():
    tech = {"score": -0.9, "atr": 1.0, "close": 100.0}
    poly = {"score": -0.8, "confidence": 0.8}
    result = compute_signal(tech, volume_score=-0.5, poly_result=poly)
    assert result["direction"] == "short"


def test_signal_neutral_when_blend_is_weak():
    tech = {"score": 0.2, "atr": 1.0, "close": 100.0}
    poly = {"score": 0.0, "confidence": 0.0}
    result = compute_signal(tech, volume_score=0.1, poly_result=poly)
    assert result["direction"] == "neutral"


def test_signal_ignores_polymarket_when_its_confidence_is_zero():
    tech = {"score": 0.3, "atr": 1.0, "close": 100.0}
    # a huge polymarket score with 0 confidence shouldn't push this over the
    # 0.5 threshold on its own
    poly = {"score": 1.0, "confidence": 0.0}
    result = compute_signal(tech, volume_score=0.0, poly_result=poly)
    assert result["direction"] == "neutral"


def test_signal_includes_component_breakdown_for_display():
    tech = {"score": 0.41, "atr": 1.0, "close": 100.0}
    poly = {"score": 0.2, "confidence": 0.5}
    news = {"score": -0.3, "confidence": 0.4}
    result = compute_signal(tech, volume_score=0.1, poly_result=poly, news_result=news)
    assert result["components"] == {"technical": 0.41, "volume": 0.1,
                                     "polymarket": 0.2, "polymarket_confidence": 0.5,
                                     "news": -0.3, "news_confidence": 0.4}


def test_signal_news_defaults_to_zero_when_not_provided():
    tech = {"score": 0.41, "atr": 1.0, "close": 100.0}
    poly = {"score": 0.0, "confidence": 0.0}
    result = compute_signal(tech, volume_score=0.0, poly_result=poly)
    assert result["components"]["news"] == 0.0
    assert result["components"]["news_confidence"] == 0.0


# -- roi_pct / should_hourly_cutoff ---------------------------------------------------------

def test_roi_pct_basic():
    assert roi_pct(unrealized_pnl=6.0, margin=50.0) == 12.0
    assert roi_pct(unrealized_pnl=-2.5, margin=50.0) == -5.0


def test_roi_pct_zero_margin_is_safe():
    assert roi_pct(unrealized_pnl=5.0, margin=0.0) == 0.0


def test_hourly_cutoff_fires_once_past_the_first_hour_if_still_negative():
    should_close, mark = should_hourly_cutoff(hours_open=1.1, roi_pct=-1.0, last_hour_checked=0)
    assert should_close
    assert mark == 1


def test_hourly_cutoff_does_not_fire_before_one_hour():
    should_close, mark = should_hourly_cutoff(hours_open=0.9, roi_pct=-1.0, last_hour_checked=0)
    assert not should_close
    assert mark == 0


def test_hourly_cutoff_does_not_fire_if_profitable_at_the_mark():
    should_close, mark = should_hourly_cutoff(hours_open=1.2, roi_pct=3.0, last_hour_checked=0)
    assert not should_close
    assert mark == 1  # still advances the mark so this hour isn't re-checked


def test_hourly_cutoff_only_checks_each_hour_mark_once():
    # already checked hour 1 -- a dip within the SAME hour shouldn't re-trigger
    should_close, mark = should_hourly_cutoff(hours_open=1.5, roi_pct=-3.0, last_hour_checked=1)
    assert not should_close
    assert mark == 1


def test_hourly_cutoff_fires_again_at_the_second_hour_mark():
    should_close, mark = should_hourly_cutoff(hours_open=2.05, roi_pct=-0.5, last_hour_checked=1)
    assert should_close
    assert mark == 2


# -- AggressiveBTCStrategy (integration, mocked client) ---------------------------------------------------------

class FakeInst:
    qty_step = 0.001
    min_qty = 0.001
    tick_size = 0.1
    max_leverage = 100.0


def _make_strategy(tmp_path):
    state = StateStore(str(tmp_path / "aggressive_btc_state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 1)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 3)
    poly_signal = MagicMock()
    poly_signal.score.return_value = {"score": 0.0, "confidence": 0.0}
    news_signal = MagicMock()
    news_signal.score_for_symbol.return_value = {"score": 0.0, "confidence": 0.0}
    return (AggressiveBTCStrategy(client, state, notifier, poly_signal, news_signal),
            state, client, poly_signal)


def test_get_signal_wires_news_signal_into_the_blend(tmp_path, monkeypatch):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    client.get_klines.return_value = [{"ts": 1, "open": 1, "high": 1, "low": 1,
                                        "close": 100.0, "volume": 1}]
    monkeypatch.setattr(aggressive_btc.technical, "score_timeframe",
                         lambda candles, cfg: {"score": 0.5, "atr": 1.0, "close": 100.0})
    monkeypatch.setattr(aggressive_btc.technical, "volume_score", lambda candles: 0.0)
    poly_signal.score.return_value = {"score": 0.0, "confidence": 0.0}
    strategy.news_signal.score_for_symbol.return_value = {"score": -1.0, "confidence": 1.0}

    signal = strategy.get_signal()

    strategy.news_signal.score_for_symbol.assert_called_once_with(aggressive_btc.SYMBOL)
    # 0.60*0.5 + 0.15*0 + 0.10*0 + 0.15*(-1.0*1.0) = 0.30 - 0.15 = 0.15
    assert round(signal["score"], 2) == 0.15


def test_try_enter_opens_a_long_on_a_strong_bullish_signal(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }

    strategy.try_enter()

    client.open_position.assert_called_once()
    trade = state.get_trade(SYMBOL)
    assert trade is not None
    assert trade["side"] == "long"
    assert trade["margin"] == 50.0  # 50% of 100 equity
    assert state.get_skip_reason(SYMBOL) is None


def test_try_enter_truncates_leverage_to_a_whole_number_before_pricing_tp_sl(tmp_path):
    # atr_pct = 2.24/140*100 = 1.6 -> compute_leverage gives 7.5 (fractional,
    # within the [6,10] range so it isn't clipped to a whole number by chance).
    # Bybit's set_leverage only ever accepts a whole number, so the bot must use
    # that same truncated 7x (not 7.5x) when it works out TP/SL, or the
    # realized ROI% at TP/SL silently drifts from the intended 12%/-5%.
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 2.24, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }

    strategy.try_enter()

    client.set_leverage.assert_called_once()
    requested_leverage = client.set_leverage.call_args[0][1]
    assert requested_leverage == 7.0  # floor(7.5), not the fractional value

    trade = state.get_trade(SYMBOL)
    expected = compute_tp_sl_prices(140.0, "long", 7.0)
    assert trade["current_tp"] == round(expected["tp"], 1)
    assert trade["current_sl"] == round(expected["sl"], 1)


def test_try_enter_clears_a_previously_set_skip_reason_on_successful_entry(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    state.set_skip_reason(SYMBOL, "관망 중 - 신호 약함 (stale)")
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }

    strategy.try_enter()

    assert state.get_skip_reason(SYMBOL) is None


def test_try_enter_skips_when_signal_is_neutral(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {
        "direction": "neutral", "score": 0.0, "atr": 1.0, "close": 100.0,
        "components": {"technical": 0.1, "volume": -0.05, "polymarket": 0.0, "polymarket_confidence": 0.0},
    }
    client.get_equity_usdt.return_value = 100.0

    strategy.try_enter()

    client.open_position.assert_not_called()
    assert state.get_trade(SYMBOL) is None
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "관망 중" in reason["reason"]
    assert "T:+0.10" in reason["reason"]
    assert "V:-0.05" in reason["reason"]


def test_try_enter_sets_skip_reason_when_no_margin_available(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 0.0

    strategy.try_enter()

    client.open_position.assert_not_called()
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "여유 증거금 없음" in reason["reason"]


def test_try_enter_sets_skip_reason_when_order_sizing_fails(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0

    import bot.aggressive_btc as agg_mod

    class FakeSizing:
        ok = False
        reason = "below minimum notional"
        qty = 0.0

    original_compute = agg_mod.compute_qty_fixed_margin
    agg_mod.compute_qty_fixed_margin = lambda **kwargs: FakeSizing()
    try:
        strategy.try_enter()
    finally:
        agg_mod.compute_qty_fixed_margin = original_compute

    client.open_position.assert_not_called()
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "주문 크기 조건 미충족" in reason["reason"]


def test_try_enter_bumps_up_to_the_minimum_entry_size_when_50pct_margin_is_too_small(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    # equity small enough that MARGIN_PCT_OF_EQUITY (50%) of it can't clear the
    # 5 USDT minimum order notional at the leverage this signal implies.
    client.get_equity_usdt.return_value = 0.5
    client.get_available_balance_usdt.return_value = 1_000_000.0  # plenty available
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }

    strategy.try_enter()

    client.open_position.assert_called_once()
    trade = state.get_trade(SYMBOL)
    assert trade is not None
    assert trade["margin"] > 0.25  # bumped up above the plain 50%-of-equity amount (0.25)
    assert trade["margin"] * trade["leverage"] >= 5.0 - 1e-6  # clears MIN_ORDER_NOTIONAL_USDT
    assert state.get_skip_reason(SYMBOL) is None


def test_try_enter_still_skips_if_even_the_minimum_entry_size_is_unaffordable(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 0.5
    client.get_available_balance_usdt.return_value = 0.1  # not even enough for the minimum
    client.get_last_price.return_value = 140.0

    strategy.try_enter()

    client.open_position.assert_not_called()
    assert state.get_trade(SYMBOL) is None
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "주문 크기 조건 미충족" in reason["reason"]


def test_try_enter_closes_immediately_if_sl_tp_fails_to_attach(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 0.0, "take_profit": 0.0,  # never attached
    }

    strategy.try_enter()

    client.close_position.assert_called_once()
    assert state.get_trade(SYMBOL) is None
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "SL/TP 설정 실패" in reason["reason"]


def test_manage_open_position_closes_on_hourly_cutoff(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    state.set_trade(SYMBOL, {
        "symbol": SYMBOL, "side": "long", "entry_price": 100.0, "qty": 1.0,
        "margin": 50.0, "leverage": 12.0, "current_sl": 90.0, "current_tp": 101.0,
        "opened_at": time.time() - 3700, "last_hour_checked": 0,  # just past 1h
    })
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": -1.0, "unrealized_pnl": -1.0, "position_idx": 0,
        "stop_loss": 90.0, "take_profit": 101.0,
    }
    client.close_position.return_value = {}
    client.get_closed_pnl.return_value = None
    client.get_last_price.return_value = 99.5

    strategy.manage_open_position()

    client.close_position.assert_called_once()
    assert state.get_trade(SYMBOL) is None
    history = state.snapshot()["history"]
    assert history[-1]["reason"] == "hourly_cutoff"


def test_manage_open_position_keeps_a_profitable_trade_open_past_the_hour(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    state.set_trade(SYMBOL, {
        "symbol": SYMBOL, "side": "long", "entry_price": 100.0, "qty": 1.0,
        "margin": 50.0, "leverage": 12.0, "current_sl": 90.0, "current_tp": 101.0,
        "opened_at": time.time() - 3700, "last_hour_checked": 0,
    })
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 2.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 101.0,
    }

    strategy.manage_open_position()

    client.close_position.assert_not_called()
    trade = state.get_trade(SYMBOL)
    assert trade is not None
    assert trade["last_hour_checked"] == 1


def test_manage_open_position_settles_when_sl_tp_already_closed_it(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    state.set_trade(SYMBOL, {
        "symbol": SYMBOL, "side": "long", "entry_price": 100.0, "qty": 1.0,
        "margin": 50.0, "leverage": 12.0, "current_sl": 90.0, "current_tp": 101.0,
        "opened_at": time.time() - 60, "last_hour_checked": 0,
    })
    client.get_position.return_value = None  # already closed on the exchange side
    client.get_closed_pnl.return_value = {
        "closed_pnl": 6.0, "avg_exit_price": 101.0, "updated_time_ms": 99999999999999,
    }

    strategy.manage_open_position()

    client.close_position.assert_not_called()  # nothing left to close
    assert state.get_trade(SYMBOL) is None
    history = state.snapshot()["history"]
    assert history[-1]["pnl"] == 6.0


def test_try_enter_noop_while_a_position_is_already_open(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    state.set_trade(SYMBOL, {
        "symbol": SYMBOL, "side": "long", "entry_price": 100.0, "qty": 1.0,
        "margin": 50.0, "leverage": 12.0, "current_sl": 90.0, "current_tp": 101.0,
        "opened_at": time.time(), "last_hour_checked": 0,
    })

    strategy.try_enter()

    client.get_klines.assert_not_called()


# -- _resolve_credentials ---------------------------------------------------------

_SECRETS = Secrets(bybit_api_key="live-key", bybit_api_secret="live-secret", bybit_testnet=False,
                    newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                    dashboard_token="t")


def test_resolve_credentials_reuses_live_keys_when_going_live(monkeypatch):
    monkeypatch.setattr(aggressive_btc, "TESTNET", False)
    result = _resolve_credentials(_SECRETS)
    assert result == ("live-key", "live-secret")


def test_resolve_credentials_uses_dedicated_testnet_keys_when_set(monkeypatch):
    monkeypatch.setattr(aggressive_btc, "TESTNET", True)
    monkeypatch.setenv("AGGRESSIVE_BTC_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("AGGRESSIVE_BTC_TESTNET_API_SECRET", "test-secret")
    result = _resolve_credentials(_SECRETS)
    assert result == ("test-key", "test-secret")


def test_resolve_credentials_refuses_testnet_without_dedicated_keys(monkeypatch):
    """A live-account key cannot authenticate against the testnet host -- this
    must never silently fall back to the main bot's live BYBIT_API_KEY.
    """
    monkeypatch.setattr(aggressive_btc, "TESTNET", True)
    monkeypatch.delenv("AGGRESSIVE_BTC_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("AGGRESSIVE_BTC_TESTNET_API_SECRET", raising=False)
    assert _resolve_credentials(_SECRETS) is None
