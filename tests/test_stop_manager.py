import time

from bot.risk import stop_manager

CFG = {
    "atr_sl_multiplier": 1.5,
    "atr_tp_multiplier": 2.5,
    "breakeven_after_rr": 0.5,
    "trail_activation_rr": 1.0,
    "trail_atr_multiplier": 1.2,
    "tp_extend_atr_step": 0.75,
    "max_tp_extensions": 3,
    "flash_move_atr_mult": 1.0,
    "flash_move_min_pct": 0.8,
    "flash_move_max_pct": 3.0,
    "flash_move_window_sec": 45,
    "reversal_exit_score": 0.4,
    "reversal_exit_confidence": 0.6,
    "min_confidence_to_enter": 0.55,
    "stale_exit_after_min": 60,
    "stale_exit_after_min_no_pressure": 240,
    "stale_exit_max_move_pct": 0.5,
}


def test_new_trade_long_sets_sl_tp_below_above_entry():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    assert trade["initial_sl"] == 100 - 2 * 1.5
    assert trade["initial_tp"] == 100 + 2 * 2.5
    assert trade["risk_distance"] == 3.0
    assert trade["entry_atr"] == 2


def test_new_trade_short_sets_sl_tp_above_below_entry():
    trade = stop_manager.new_trade("BTCUSDT", "short", entry_price=100, qty=1, atr=2, cfg=CFG)
    assert trade["initial_sl"] == 100 + 3.0
    assert trade["initial_tp"] == 100 - 5.0


def test_breakeven_moves_sl_past_entry_by_the_configured_buffer():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    neutral_signal = {"direction": "neutral", "score": 0.0, "confidence": 0.0}
    # profit_r = (101.6 - 100) / 3 = 0.53 >= breakeven_after_rr(0.5)
    result = stop_manager.update_trailing_and_tp(trade, 101.6, atr=2, agg_signal=neutral_signal, cfg=CFG)
    assert result["sl_changed"]
    # not exactly entry (100) -- buffered past it by atr(2)*breakeven_buffer_atr_mult(0.2) = 0.4
    assert trade["current_sl"] == 100.4
    assert trade["breakeven_moved"]


def test_breakeven_buffer_defaults_to_a_sensible_value_when_unconfigured():
    cfg = {k: v for k, v in CFG.items() if k != "breakeven_buffer_atr_mult"}
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=cfg)
    neutral_signal = {"direction": "neutral", "score": 0.0, "confidence": 0.0}
    stop_manager.update_trailing_and_tp(trade, 101.6, atr=2, agg_signal=neutral_signal, cfg=cfg)
    assert trade["current_sl"] > 100  # buffered past entry, not exactly at it


def test_trailing_never_loosens_stop():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    neutral_signal = {"direction": "neutral", "score": 0.0, "confidence": 0.0}
    stop_manager.update_trailing_and_tp(trade, 106, atr=2, agg_signal=neutral_signal, cfg=CFG)
    sl_after_first = trade["current_sl"]
    # price pulls back but is still above entry -- SL must not move down/backward.
    stop_manager.update_trailing_and_tp(trade, 103, atr=2, agg_signal=neutral_signal, cfg=CFG)
    assert trade["current_sl"] >= sl_after_first


def test_tp_extends_when_near_target_and_signal_still_bullish():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    strong_bullish = {"direction": "long", "score": 0.6, "confidence": 0.8}
    # tp is 105; price at 104.5 is within atr*0.5=1 of tp.
    result = stop_manager.update_trailing_and_tp(trade, 104.5, atr=2, agg_signal=strong_bullish, cfg=CFG)
    assert result["tp_changed"]
    assert trade["current_tp"] > 105
    assert trade["tp_extensions_used"] == 1


def test_flash_move_detected_against_long_position():
    tracker = stop_manager.FlashMoveTracker()
    tracker.record("BTCUSDT", 100.0, window_sec=45)
    tracker.record("BTCUSDT", 98.0, window_sec=45)  # -2% within window
    # entry_atr_pct=1.2 -> threshold clamped to 1.2 (within [0.8, 3.0])
    assert stop_manager.check_flash_move(tracker, "BTCUSDT", "long", CFG, entry_atr_pct=1.2)


def test_flash_move_not_triggered_for_small_moves():
    tracker = stop_manager.FlashMoveTracker()
    tracker.record("BTCUSDT", 100.0, window_sec=45)
    tracker.record("BTCUSDT", 99.5, window_sec=45)  # -0.5%, below 1.2% threshold
    assert not stop_manager.check_flash_move(tracker, "BTCUSDT", "long", CFG, entry_atr_pct=1.2)


def test_flash_move_threshold_widens_for_high_atr_symbol_but_is_capped():
    tracker = stop_manager.FlashMoveTracker()
    tracker.record("VOLATILEUSDT", 100.0, window_sec=45)
    tracker.record("VOLATILEUSDT", 97.5, window_sec=45)  # -2.5% within window
    # entry_atr_pct=5.0 * mult(1.0) = 5.0, clamped down to max_pct(3.0) -> -2.5% doesn't clear it
    assert not stop_manager.check_flash_move(tracker, "VOLATILEUSDT", "long", CFG, entry_atr_pct=5.0)
    tracker.record("VOLATILEUSDT", 96.5, window_sec=45)  # now -3.5%, past the 3.0% cap
    assert stop_manager.check_flash_move(tracker, "VOLATILEUSDT", "long", CFG, entry_atr_pct=5.0)


def test_flash_move_threshold_narrows_for_low_atr_symbol_but_is_floored():
    tracker = stop_manager.FlashMoveTracker()
    tracker.record("STABLEUSDT", 100.0, window_sec=45)
    tracker.record("STABLEUSDT", 99.5, window_sec=45)  # -0.5%
    # entry_atr_pct=0.1 * mult(1.0) = 0.1, floored up to min_pct(0.8) -> -0.5% doesn't clear it
    assert not stop_manager.check_flash_move(tracker, "STABLEUSDT", "long", CFG, entry_atr_pct=0.1)
    tracker.record("STABLEUSDT", 99.1, window_sec=45)  # now -0.9%, past the 0.8% floor
    assert stop_manager.check_flash_move(tracker, "STABLEUSDT", "long", CFG, entry_atr_pct=0.1)


def test_signal_reversal_exit_triggers_on_strong_opposite_signal():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    strong_bearish = {"direction": "short", "score": -0.5, "confidence": 0.7}
    assert stop_manager.check_signal_reversal(trade, strong_bearish, CFG)


def test_signal_reversal_ignored_when_confidence_too_low():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    weak_bearish = {"direction": "short", "score": -0.5, "confidence": 0.3}
    assert not stop_manager.check_signal_reversal(trade, weak_bearish, CFG)


def test_stale_position_triggers_after_timeout_with_no_movement():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    trade["opened_at"] = time.time() - 61 * 60  # opened 61 minutes ago
    assert stop_manager.check_stale_position(trade, current_price=100.3, cfg=CFG)  # 0.3% move


def test_stale_position_not_triggered_before_timeout():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    trade["opened_at"] = time.time() - 10 * 60  # only 10 minutes ago
    assert not stop_manager.check_stale_position(trade, current_price=100.1, cfg=CFG)


def test_stale_position_not_triggered_if_price_actually_moved():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    trade["opened_at"] = time.time() - 61 * 60
    assert not stop_manager.check_stale_position(trade, current_price=102.0, cfg=CFG)  # 2% move


def test_stale_position_disabled_when_timeout_is_zero():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    trade["opened_at"] = time.time() - 1000 * 60
    cfg = {**CFG, "stale_exit_after_min": 0}
    assert not stop_manager.check_stale_position(trade, current_price=100.0, cfg=cfg)


def test_stale_position_waits_longer_without_capital_pressure():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    trade["opened_at"] = time.time() - 90 * 60  # past the normal 60min, well under 240min
    assert stop_manager.check_stale_position(trade, current_price=100.1, cfg=CFG, capital_pressure=True)
    assert not stop_manager.check_stale_position(trade, current_price=100.1, cfg=CFG, capital_pressure=False)


def test_stale_position_fires_without_pressure_once_past_the_longer_timeout():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    trade["opened_at"] = time.time() - 241 * 60  # past both timeouts
    assert stop_manager.check_stale_position(trade, current_price=100.1, cfg=CFG, capital_pressure=False)


# -- adverse-move stop tightening -----------------------------------------------------------------

ADVERSE_CFG = {
    **CFG,
    "adverse_tighten_start_rr": 0.5,
    "adverse_tighten_confidence_floor": 0.5,
    "adverse_tighten_score_floor": 0.15,
    "adverse_tighten_atr_multiplier": 0.5,
}


def test_adverse_tighten_pulls_sl_in_when_losing_and_signal_faded():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    # initial_sl = 100 - 2*1.5 = 97; price at 98.3 -> profit_r = (98.3-100)/3 = -0.57 <= -0.5
    faded_signal = {"direction": "long", "score": 0.05, "confidence": 0.9}  # score below floor
    result = stop_manager.update_trailing_and_tp(trade, 98.3, atr=2, agg_signal=faded_signal, cfg=ADVERSE_CFG)

    assert result["sl_changed"]
    assert "adverse_tighten" in result["reason"]
    # candidate = 98.3 - 2*0.5 = 97.3, tighter (higher) than the original 97 SL --
    # caps this loss at ~0.9R instead of riding to the full 1.0R initial SL.
    assert trade["current_sl"] == 97.3


def test_adverse_tighten_triggers_on_low_confidence_even_if_direction_unchanged():
    trade = stop_manager.new_trade("BTCUSDT", "short", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    # initial_sl = 100 + 3 = 103; price at 101.7 -> profit_r = (100-101.7)/3 = -0.57 <= -0.5
    low_conf_signal = {"direction": "short", "score": 0.5, "confidence": 0.2}  # confidence below floor
    result = stop_manager.update_trailing_and_tp(trade, 101.7, atr=2, agg_signal=low_conf_signal, cfg=ADVERSE_CFG)

    assert result["sl_changed"]
    assert trade["current_sl"] == 102.7  # 101.7 + 2*0.5, tighter than the original 103


def test_adverse_tighten_triggers_on_outright_reversal():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    flipped_signal = {"direction": "short", "score": -0.6, "confidence": 0.9}
    result = stop_manager.update_trailing_and_tp(trade, 98.3, atr=2, agg_signal=flipped_signal, cfg=ADVERSE_CFG)
    assert result["sl_changed"]
    assert "adverse_tighten" in result["reason"]


def test_adverse_tighten_not_triggered_while_signal_still_strongly_supports_the_trade():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    strong_bullish = {"direction": "long", "score": 0.6, "confidence": 0.8}
    result = stop_manager.update_trailing_and_tp(trade, 98.3, atr=2, agg_signal=strong_bullish, cfg=ADVERSE_CFG)
    assert not result["sl_changed"]
    assert trade["current_sl"] == 97.0  # untouched initial SL


def test_adverse_tighten_not_triggered_before_the_rr_threshold():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    # price at 99.5 -> profit_r = (99.5-100)/3 = -0.17, shallower than -0.5 threshold
    faded_signal = {"direction": "long", "score": 0.0, "confidence": 0.0}
    result = stop_manager.update_trailing_and_tp(trade, 99.5, atr=2, agg_signal=faded_signal, cfg=ADVERSE_CFG)
    assert not result["sl_changed"]


def test_adverse_tighten_never_loosens_stop():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    faded_signal = {"direction": "long", "score": 0.0, "confidence": 0.0}
    stop_manager.update_trailing_and_tp(trade, 98.3, atr=2, agg_signal=faded_signal, cfg=ADVERSE_CFG)
    tightened_sl = trade["current_sl"]  # 97.3
    # price keeps falling further against the position -- the freshly computed
    # candidate (97.5 - 2*0.5 = 96.5) is now looser (lower) than what's already
    # locked in, since it's anchored off a lower current price. Must not revert.
    stop_manager.update_trailing_and_tp(trade, 97.5, atr=2, agg_signal=faded_signal, cfg=ADVERSE_CFG)
    assert trade["current_sl"] == tightened_sl


def test_adverse_tighten_per_trade_override_triggers_earlier_than_global_default():
    """A trade flagged at entry as somewhat chase-extended (see
    strategy._enter_trend's chase_caution_atr_mult check) carries its own
    tighter adverse_tighten_start_rr_override -- it should react to a
    shallower drawdown than the global 0.5 default would.
    """
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    trade["adverse_tighten_start_rr_override"] = 0.25
    # price at 99.1 -> profit_r = (99.1-100)/3 = -0.30 -- past the 0.25 override
    # but NOT past the global 0.5 default.
    faded_signal = {"direction": "long", "score": 0.0, "confidence": 0.0}
    result = stop_manager.update_trailing_and_tp(trade, 99.1, atr=2, agg_signal=faded_signal, cfg=ADVERSE_CFG)

    assert result["sl_changed"]
    assert "adverse_tighten" in result["reason"]


def test_adverse_tighten_without_override_ignores_the_shallower_drawdown():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    # no override set on this trade -- same 99.1 price stays under the global 0.5 threshold
    faded_signal = {"direction": "long", "score": 0.0, "confidence": 0.0}
    result = stop_manager.update_trailing_and_tp(trade, 99.1, atr=2, agg_signal=faded_signal, cfg=ADVERSE_CFG)

    assert not result["sl_changed"]


def test_adverse_tighten_override_fires_even_if_the_signal_still_looks_supportive():
    """The whole point of the override: a chase-flagged trade was already
    distrusted at entry, so it shouldn't need a SECOND confirmation (the live
    signal visibly weakening) before tightening -- that lags a fast snap-back
    reversal. Observed live: a chase-flagged WLDUSDT trade rode to its full
    initial SL with no adverse_tighten ever firing because the cached signal
    never visibly flipped in time.
    """
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    trade["adverse_tighten_start_rr_override"] = 0.25
    # price at 99.1 -> profit_r = -0.30, past the 0.25 override
    strong_supportive_signal = {"direction": "long", "score": 0.9, "confidence": 0.9}
    result = stop_manager.update_trailing_and_tp(trade, 99.1, atr=2, agg_signal=strong_supportive_signal,
                                                  cfg=ADVERSE_CFG)

    assert result["sl_changed"]
    assert "adverse_tighten" in result["reason"]


def test_adverse_tighten_without_override_still_requires_a_faded_signal():
    """Non-chase-flagged trades are unaffected by the override bypass -- they
    still need BOTH the RR threshold and a visibly weakened signal, same as
    before.
    """
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=ADVERSE_CFG)
    # no override on this trade; price well past the global 0.5 threshold
    strong_supportive_signal = {"direction": "long", "score": 0.9, "confidence": 0.9}
    result = stop_manager.update_trailing_and_tp(trade, 98.3, atr=2, agg_signal=strong_supportive_signal,
                                                  cfg=ADVERSE_CFG)

    assert not result["sl_changed"]
