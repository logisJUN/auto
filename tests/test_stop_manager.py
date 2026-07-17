from bot.risk import stop_manager

CFG = {
    "atr_sl_multiplier": 1.5,
    "atr_tp_multiplier": 2.5,
    "breakeven_after_rr": 0.5,
    "trail_activation_rr": 1.0,
    "trail_atr_multiplier": 1.2,
    "tp_extend_atr_step": 0.75,
    "max_tp_extensions": 3,
    "flash_move_pct": 1.2,
    "flash_move_window_sec": 45,
    "reversal_exit_score": 0.4,
    "reversal_exit_confidence": 0.6,
    "min_confidence_to_enter": 0.55,
}


def test_new_trade_long_sets_sl_tp_below_above_entry():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    assert trade["initial_sl"] == 100 - 2 * 1.5
    assert trade["initial_tp"] == 100 + 2 * 2.5
    assert trade["risk_distance"] == 3.0


def test_new_trade_short_sets_sl_tp_above_below_entry():
    trade = stop_manager.new_trade("BTCUSDT", "short", entry_price=100, qty=1, atr=2, cfg=CFG)
    assert trade["initial_sl"] == 100 + 3.0
    assert trade["initial_tp"] == 100 - 5.0


def test_breakeven_moves_sl_to_entry_once_profitable_enough():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    neutral_signal = {"direction": "neutral", "score": 0.0, "confidence": 0.0}
    # profit_r = (101.6 - 100) / 3 = 0.53 >= breakeven_after_rr(0.5)
    result = stop_manager.update_trailing_and_tp(trade, 101.6, atr=2, agg_signal=neutral_signal, cfg=CFG)
    assert result["sl_changed"]
    assert trade["current_sl"] == 100
    assert trade["breakeven_moved"]


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
    assert stop_manager.check_flash_move(tracker, "BTCUSDT", "long", CFG)


def test_flash_move_not_triggered_for_small_moves():
    tracker = stop_manager.FlashMoveTracker()
    tracker.record("BTCUSDT", 100.0, window_sec=45)
    tracker.record("BTCUSDT", 99.5, window_sec=45)  # -0.5%, below 1.2% threshold
    assert not stop_manager.check_flash_move(tracker, "BTCUSDT", "long", CFG)


def test_signal_reversal_exit_triggers_on_strong_opposite_signal():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    strong_bearish = {"direction": "short", "score": -0.5, "confidence": 0.7}
    assert stop_manager.check_signal_reversal(trade, strong_bearish, CFG)


def test_signal_reversal_ignored_when_confidence_too_low():
    trade = stop_manager.new_trade("BTCUSDT", "long", entry_price=100, qty=1, atr=2, cfg=CFG)
    weak_bearish = {"direction": "short", "score": -0.5, "confidence": 0.3}
    assert not stop_manager.check_signal_reversal(trade, weak_bearish, CFG)
