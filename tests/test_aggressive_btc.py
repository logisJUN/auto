"""bot/aggressive_btc.py: a standalone, SEPARATE multi-symbol aggressive
strategy (SOXLUSDT plus 38 non-leveraged stock tokens; 12% target / 1h, -5%
SL, hourly negative-ROI cutoff, 50% margin per trade, global cap of one open
position at a time across every configured symbol). Deliberately isolated
from the main bot/strategy.py -- these tests cover its own pure logic and its
own strategy class in isolation, using SOXL_CONFIG (via the SYMBOL/_make_strategy
helpers below) as the representative config unless a test specifically needs
another symbol.
"""
import time
from unittest.mock import MagicMock

import pytest

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
from bot.exchange.bybit_client import BybitAPIError, BybitClient
from bot.logger import read_recent_decisions
from bot.notify import Notifier
from bot.state import StateStore


# -- AGGRESSIVE_CONFIGS ---------------------------------------------------------

def test_aggressive_configs_has_soxl_plus_38_distinct_stock_symbols_across_two_pools():
    symbols = [sc.symbol for sc in aggressive_btc.AGGRESSIVE_CONFIGS]
    assert symbols[0] == "SOXLUSDT"
    assert "SNXXUSDT" not in symbols  # removed -- unpredictable embedded-leverage PNL
    assert len(symbols) == 39
    assert len(set(symbols)) == 39  # no duplicates

    semi_ai_configs = aggressive_btc.SEMI_AI_CONFIGS
    stock_configs = aggressive_btc.STOCK_CONFIGS
    assert len(semi_ai_configs) == 20
    assert len(stock_configs) == 18
    assert "NVDAUSDT" in [sc.symbol for sc in semi_ai_configs]
    assert "AVGOUSDT" in [sc.symbol for sc in semi_ai_configs]  # moved from the general pool -- it's a chipmaker too

    for sc in semi_ai_configs + stock_configs:
        # No embedded leverage on these (unlike SOXLUSDT) -- allowed a higher
        # range, but still well under the original 24x/25x crypto design.
        assert sc.min_leverage == 8.0
        assert sc.max_leverage == 15.0
        assert sc.leverage_hard_cap == 15.0
        assert sc.margin_pct_of_equity == 50.0  # safe only because of the global 1-position cap

    # The two pools use genuinely different news_cfg objects (separate RSS
    # pools/NewsSignal instances), not just coincidentally-equal dicts.
    semi_ai_news_ids = {id(sc.news_cfg) for sc in semi_ai_configs}
    stock_news_ids = {id(sc.news_cfg) for sc in stock_configs}
    assert len(semi_ai_news_ids) == 1
    assert len(stock_news_ids) == 1
    assert semi_ai_news_ids != stock_news_ids


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
    # with news weighted at 0.65, technical/volume/polymarket alone (even
    # maxed out) can no longer clear the entry bar -- strong news is required.
    tech = {"score": 0.9, "atr": 1.0, "close": 100.0}
    poly = {"score": 0.8, "confidence": 0.8}
    news = {"score": 0.8, "confidence": 0.8}
    result = compute_signal(tech, volume_score=0.5, poly_result=poly, news_result=news)
    assert result["direction"] == "long"


def test_signal_short_when_blend_strongly_bearish():
    tech = {"score": -0.9, "atr": 1.0, "close": 100.0}
    poly = {"score": -0.8, "confidence": 0.8}
    news = {"score": -0.8, "confidence": 0.8}
    result = compute_signal(tech, volume_score=-0.5, poly_result=poly, news_result=news)
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
    news = {"score": -0.3, "confidence": 0.4, "bullish_count": 1, "bearish_count": 2, "neutral_count": 3}
    result = compute_signal(tech, volume_score=0.1, poly_result=poly, news_result=news)
    assert result["components"] == {"technical": 0.41, "volume": 0.1,
                                     "polymarket": 0.2, "polymarket_confidence": 0.5,
                                     "news": -0.3, "news_confidence": 0.4,
                                     "news_bullish": 1, "news_bearish": 2, "news_neutral": 3}


def test_signal_news_defaults_to_zero_when_not_provided():
    tech = {"score": 0.41, "atr": 1.0, "close": 100.0}
    poly = {"score": 0.0, "confidence": 0.0}
    result = compute_signal(tech, volume_score=0.0, poly_result=poly)
    assert result["components"]["news"] == 0.0
    assert result["components"]["news_confidence"] == 0.0
    assert result["components"]["news_bullish"] == 0
    assert result["components"]["news_bearish"] == 0
    assert result["components"]["news_neutral"] == 0


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
    return (AggressiveBTCStrategy(client, state, notifier, poly_signal, news_signal,
                                   cfg=aggressive_btc.SOXL_CONFIG, log_dir=str(tmp_path / "logs")),
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
    # 0.10*0.5 + 0.15*0 + 0.10*0 + 0.65*(-1.0*1.0) = 0.05 - 0.65 = -0.60
    assert round(signal["score"], 3) == -0.60


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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

    strategy.try_enter()

    client.open_position.assert_called_once()
    trade = state.get_trade(SYMBOL)
    assert trade is not None
    assert trade["side"] == "long"
    assert trade["margin"] == 50.0  # 50% of 100 equity
    assert state.get_skip_reason(SYMBOL) is None


def test_try_enter_logs_an_entry_decision_on_success(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {
        "direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0,
        "components": {"technical": 0.5, "volume": 0.2, "polymarket": 0.0,
                        "polymarket_confidence": 0.0, "news": 0.1, "news_confidence": 0.3},
    }
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

    strategy.try_enter()

    decisions = read_recent_decisions(strategy.log_dir)
    entries = [d for d in decisions if d.get("event") == "entry"]
    assert len(entries) == 1
    assert entries[0]["symbol"] == SYMBOL
    assert entries[0]["side"] == "long"
    assert entries[0]["signal"]["components"]["technical"]["score"] == 0.5


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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

    strategy.try_enter()

    assert state.get_skip_reason(SYMBOL) is None


# -- entry confirmation (2 consecutive same-direction checks) ---------------------------------------------------------

def test_try_enter_does_not_enter_on_the_first_tick_a_direction_crosses_threshold(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0

    strategy.try_enter()

    client.open_position.assert_not_called()
    assert state.get_trade(SYMBOL) is None
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "1차 확인됨" in reason["reason"]
    assert strategy._pending_entry == {"direction": "long"}


def test_try_enter_enters_on_the_second_consecutive_tick_with_the_same_direction(tmp_path):
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

    strategy.try_enter()  # 1st tick -- just registers the pending confirmation
    strategy.try_enter()  # 2nd tick, same direction -- actually enters

    client.open_position.assert_called_once()
    trade = state.get_trade(SYMBOL)
    assert trade is not None
    assert trade["side"] == "long"
    assert strategy._pending_entry is None  # cleared once acted on


def test_try_enter_restarts_confirmation_if_the_direction_flips(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0

    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    strategy.try_enter()  # 1st tick: pending long
    assert strategy._pending_entry == {"direction": "long"}

    strategy.get_signal = lambda: {"direction": "short", "score": -0.8, "atr": 1.5, "close": 140.0}
    strategy.try_enter()  # direction flipped -- must NOT enter short immediately

    client.open_position.assert_not_called()
    assert strategy._pending_entry == {"direction": "short"}  # confirmation restarted for the new direction


def test_try_enter_clears_pending_confirmation_once_signal_goes_neutral(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    client.get_equity_usdt.return_value = 100.0

    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    strategy.try_enter()  # 1st tick: pending long
    assert strategy._pending_entry is not None

    strategy.get_signal = lambda: {
        "direction": "neutral", "score": 0.1, "atr": 1.5, "close": 140.0,
        "components": {"technical": 0.1, "volume": 0.0, "polymarket": 0.0, "polymarket_confidence": 0.0},
    }
    strategy.try_enter()
    assert strategy._pending_entry is None

    # A fresh "long" signal after the neutral gap must need its own 2 confirming
    # ticks again -- the earlier pending long must not silently still count.
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    strategy.try_enter()

    client.open_position.assert_not_called()


def test_try_enter_skips_when_signal_is_neutral(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {
        "direction": "neutral", "score": 0.0, "atr": 1.0, "close": 100.0,
        "components": {"technical": 0.1, "volume": -0.05, "polymarket": 0.0, "polymarket_confidence": 0.0,
                       "news": -0.2, "news_confidence": 0.13,
                       "news_bullish": 0, "news_bearish": 1, "news_neutral": 1},
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
    assert "긍정0/부정1/중립1" in reason["reason"]

    decisions = read_recent_decisions(strategy.log_dir)
    checks = [d for d in decisions if d.get("event") == "signal_check"]
    assert len(checks) == 1
    assert checks[0]["direction"] == "neutral"
    assert checks[0]["signal"]["components"]["technical"]["score"] == 0.1


def test_signal_check_logging_is_throttled(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {
        "direction": "neutral", "score": 0.0, "atr": 1.0, "close": 100.0,
        "components": {"technical": 0.1, "volume": 0.0, "polymarket": 0.0, "polymarket_confidence": 0.0},
    }
    client.get_equity_usdt.return_value = 100.0

    strategy.try_enter()
    strategy.try_enter()  # immediately again -- still within the throttle window

    decisions = read_recent_decisions(strategy.log_dir)
    checks = [d for d in decisions if d.get("event") == "signal_check"]
    assert len(checks) == 1  # not 2 -- throttled


def test_try_enter_sets_skip_reason_when_no_margin_available(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 0.0
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick
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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

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
    strategy._pending_entry = {"direction": "long"}  # simulate the confirming 2nd consecutive tick

    strategy.try_enter()

    client.close_position.assert_called_once()
    assert state.get_trade(SYMBOL) is None
    reason = state.get_skip_reason(SYMBOL)
    assert reason is not None
    assert "SL/TP 설정 실패" in reason["reason"]


# -- fill-price drift reconciliation ---------------------------------------------------------
# A MARKET order's actual average fill price can differ from the
# get_last_price() snapshot taken just before placing it (observed live on
# DJTUSDT: a ~0.37% stale snapshot during a fast-moving candle turned an
# intended +12%/-5% ROI target into an actual +6.5%/-10.6%) -- try_enter()
# now compares the real fill price (from get_position() after the order
# fills) against its own pre-trade estimate and re-prices SL/TP if they
# drifted meaningfully.

def test_try_enter_reprices_sl_tp_when_fill_price_drifts_from_estimate(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "short", "score": -0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0  # pre-trade estimate
    client.open_position.return_value = {}
    real_fill_price = 139.0  # >0.1% away from the 140.0 estimate -- should trigger a reprice
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Sell", "size": 1.0, "entry_price": real_fill_price,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 147.0, "take_profit": 123.2,  # as originally attached
    }
    strategy._pending_entry = {"direction": "short"}

    strategy.try_enter()

    client.update_trading_stop.assert_called_once()
    call_kwargs = client.update_trading_stop.call_args
    assert call_kwargs[0][0] == SYMBOL

    trade = state.get_trade(SYMBOL)
    assert trade is not None
    assert trade["entry_price"] == real_fill_price  # local bookkeeping uses the REAL fill price
    expected = compute_tp_sl_prices(real_fill_price, "short", trade["leverage"])
    assert trade["current_sl"] == round(expected["sl"], 1)
    assert trade["current_tp"] == round(expected["tp"], 1)


def test_try_enter_does_not_reprice_when_fill_price_is_close_to_estimate(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 140.05,  # 0.036% drift -- below the 0.1% bar
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }
    strategy._pending_entry = {"direction": "long"}

    strategy.try_enter()

    client.update_trading_stop.assert_not_called()
    trade = state.get_trade(SYMBOL)
    assert trade["entry_price"] == 140.05  # still uses the real fill price for bookkeeping either way


def test_try_enter_keeps_original_sl_tp_if_reprice_call_fails(tmp_path):
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_last_price.return_value = 140.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": SYMBOL, "side": "Buy", "size": 1.0, "entry_price": 139.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 130.0, "take_profit": 150.0,
    }
    client.update_trading_stop.side_effect = BybitAPIError("position already closed")
    strategy._pending_entry = {"direction": "long"}

    strategy.try_enter()  # must not raise, and must still record a trade

    trade = state.get_trade(SYMBOL)
    assert trade is not None
    # Falls back to the originally-computed (pre-drift-correction) SL/TP since the reprice failed.
    assert trade["current_sl"] == round(compute_tp_sl_prices(140.0, "long", trade["leverage"])["sl"], 1)
    assert trade["current_tp"] == round(compute_tp_sl_prices(140.0, "long", trade["leverage"])["tp"], 1)


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

    decisions = read_recent_decisions(strategy.log_dir)
    exits = [d for d in decisions if d.get("event") == "exit"]
    assert len(exits) == 1
    assert exits[0]["symbol"] == SYMBOL
    assert exits[0]["reason"] == "hourly_cutoff"


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


def test_try_enter_skips_when_both_long_and_short_are_already_open_elsewhere(tmp_path):
    # Capacity cap: at most one long AND one short across all configured
    # symbols (see module docstring) -- this strategy's own symbol (SOXLUSDT)
    # is flat, but two OTHER symbols already occupy both sides, so capacity
    # is full no matter what direction this symbol's own signal is --  it
    # must not even check its own signal.
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    state.set_trade("AAPLUSDT", {
        "symbol": "AAPLUSDT", "side": "long", "entry_price": 200.0, "qty": 1.0,
        "margin": 50.0, "leverage": 10.0, "current_sl": 190.0, "current_tp": 210.0,
        "opened_at": time.time(), "last_hour_checked": 0,
    })
    state.set_trade("TSLAUSDT", {
        "symbol": "TSLAUSDT", "side": "short", "entry_price": 300.0, "qty": 1.0,
        "margin": 50.0, "leverage": 10.0, "current_sl": 310.0, "current_tp": 290.0,
        "opened_at": time.time(), "last_hour_checked": 0,
    })

    strategy.try_enter()

    client.get_klines.assert_not_called()
    skip = state.get_skip_reason(SYMBOL)
    assert skip is not None
    assert "방향당 1개 제한" in skip["reason"]


def test_try_enter_skips_when_its_own_direction_matches_an_already_open_side(tmp_path):
    # Only ONE side (long) is occupied -- this symbol must still compute its
    # own signal (to know its direction), and is blocked only because its
    # direction (long) happens to match the already-occupied side.
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "long", "score": 0.8, "atr": 1.5, "close": 140.0}
    state.set_trade("AAPLUSDT", {
        "symbol": "AAPLUSDT", "side": "long", "entry_price": 200.0, "qty": 1.0,
        "margin": 50.0, "leverage": 10.0, "current_sl": 190.0, "current_tp": 210.0,
        "opened_at": time.time(), "last_hour_checked": 0,
    })

    strategy.try_enter()

    client.open_position.assert_not_called()
    skip = state.get_skip_reason(SYMBOL)
    assert skip is not None
    assert "long 방향 포지션이 이미 다른 종목에 있음" in skip["reason"]


def test_try_enter_allowed_when_its_own_direction_is_the_opposite_free_side(tmp_path):
    # AAPLUSDT holds the long side -- a short signal on this symbol is the
    # free side, so it must proceed past the capacity check (into the normal
    # 2-tick confirmation flow, not an immediate entry).
    strategy, state, client, poly_signal = _make_strategy(tmp_path)
    strategy.get_signal = lambda: {"direction": "short", "score": -0.8, "atr": 1.5, "close": 140.0}
    state.set_trade("AAPLUSDT", {
        "symbol": "AAPLUSDT", "side": "long", "entry_price": 200.0, "qty": 1.0,
        "margin": 50.0, "leverage": 10.0, "current_sl": 190.0, "current_tp": 210.0,
        "opened_at": time.time(), "last_hour_checked": 0,
    })

    strategy.try_enter()

    skip = state.get_skip_reason(SYMBOL)
    assert skip is not None
    assert "방향당 1개 제한" not in skip["reason"]  # not blocked by capacity
    assert "1차 확인됨" in skip["reason"]  # reached the normal confirmation gate instead


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


# -- _reconcile_orphaned_positions ---------------------------------------------------------
# A position can be open on the exchange with no corresponding entry in the
# shared local state -- e.g. aggressive_btc_state.json was wiped by a Render
# redeploy (no persistent disk on the free plan; this module pushes new
# commits often). Without this, tick() would route that symbol through
# try_enter() instead of manage_open_position() (no hourly-cutoff
# management), and the long/short capacity cap would no longer see it as
# occupying its side. Only a position with BOTH SL and TP already set gets
# adopted -- try_enter() always attaches both (or force-closes immediately
# if it can't), so that's the reliable signal this is a real restart-
# recovery case rather than a manually-opened position to leave alone.
from bot.aggressive_btc import _reconcile_orphaned_positions  # noqa: E402


def _orphan(symbol, side="Buy", size=2.0, entry_price=50.0, stop_loss=0, take_profit=0, leverage=10.0):
    return {"symbol": symbol, "side": side, "size": size, "entry_price": entry_price,
            "unrealized_pnl": 0.0, "position_idx": 0,
            "stop_loss": stop_loss, "take_profit": take_profit, "leverage": leverage}


def _make_reconcile_env(tmp_path):
    state = StateStore(str(tmp_path / "aggressive_btc_state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    log_dir = str(tmp_path / "logs")
    configs_by_symbol = {sc.symbol: sc for sc in aggressive_btc.AGGRESSIVE_CONFIGS}
    return client, state, notifier, log_dir, configs_by_symbol


def test_reconcile_adopts_an_orphaned_position_with_sl_tp_already_set(tmp_path):
    client, state, notifier, log_dir, configs_by_symbol = _make_reconcile_env(tmp_path)
    client.get_all_open_positions.return_value = [
        _orphan("DJTUSDT", side="Sell", size=9.2, entry_price=6.931, stop_loss=6.980, take_profit=6.901, leverage=15.0)
    ]

    _reconcile_orphaned_positions(client, state, notifier, log_dir, configs_by_symbol)

    trade = state.get_trade("DJTUSDT")
    assert trade is not None
    assert trade["side"] == "short"
    assert trade["qty"] == 9.2
    assert trade["entry_price"] == 6.931
    assert trade["current_sl"] == 6.980
    assert trade["current_tp"] == 6.901
    assert trade["leverage"] == 15.0
    assert trade["margin"] == pytest.approx(9.2 * 6.931 / 15.0)


def test_reconcile_leaves_a_position_without_sl_tp_alone_as_presumably_manual(tmp_path):
    client, state, notifier, log_dir, configs_by_symbol = _make_reconcile_env(tmp_path)
    notifier.send = MagicMock()
    client.get_all_open_positions.return_value = [_orphan("AAPLUSDT")]  # no SL/TP

    _reconcile_orphaned_positions(client, state, notifier, log_dir, configs_by_symbol)

    assert state.get_trade("AAPLUSDT") is None
    notifier.send.assert_not_called()


def test_reconcile_skips_a_symbol_already_tracked_locally(tmp_path):
    client, state, notifier, log_dir, configs_by_symbol = _make_reconcile_env(tmp_path)
    existing = {
        "symbol": "AAPLUSDT", "side": "long", "entry_price": 200.0, "qty": 1.0,
        "margin": 50.0, "leverage": 10.0, "current_sl": 190.0, "current_tp": 210.0,
        "opened_at": 123.0, "last_hour_checked": 0,
    }
    state.set_trade("AAPLUSDT", existing)
    client.get_all_open_positions.return_value = [
        _orphan("AAPLUSDT", stop_loss=195.0, take_profit=220.0)  # different values -- must NOT overwrite
    ]

    _reconcile_orphaned_positions(client, state, notifier, log_dir, configs_by_symbol)

    assert state.get_trade("AAPLUSDT")["opened_at"] == 123.0
    assert state.get_trade("AAPLUSDT")["current_sl"] == 190.0


def test_reconcile_ignores_a_symbol_not_in_the_configured_aggressive_btc_set(tmp_path):
    # BTCUSDT belongs to the main bot, not aggressive_btc -- must not be adopted here.
    client, state, notifier, log_dir, configs_by_symbol = _make_reconcile_env(tmp_path)
    client.get_all_open_positions.return_value = [_orphan("BTCUSDT", stop_loss=90.0, take_profit=110.0)]

    _reconcile_orphaned_positions(client, state, notifier, log_dir, configs_by_symbol)

    assert state.get_trade("BTCUSDT") is None


def test_reconcile_tolerates_the_exchange_call_failing(tmp_path):
    client, state, notifier, log_dir, configs_by_symbol = _make_reconcile_env(tmp_path)
    client.get_all_open_positions.side_effect = BybitAPIError("network error")

    _reconcile_orphaned_positions(client, state, notifier, log_dir, configs_by_symbol)  # must not raise

    assert state.snapshot()["trades"] == {}
