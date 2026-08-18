"""risk.max_entry_chase_atr_mult: skip a trend entry if price has already
moved this many multiples of ATR (over the recent lookback window) in the
SAME direction as the candidate trade. Every technical sub-score reads a big,
fast recent move as strong directional confirmation with no way to tell a
fresh breakout apart from an already-exhausted spike -- observed live
repeatedly (SOLUSDT, XAUUSDT, SNXXUSDT's 0-for-5 record): longs entered a few
candles after a single huge green candle, right near the local top.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.state import StateStore
from bot.strategy import Strategy

TRADE_CFG = {
    "atr_sl_multiplier": 1.5, "atr_tp_multiplier": 2.5,
    "breakeven_after_rr": 0.5, "trail_activation_rr": 1.0, "trail_atr_multiplier": 1.2,
    "tp_extend_atr_step": 0.75, "max_tp_extensions": 3,
    "flash_move_atr_mult": 1.0, "flash_move_min_pct": 0.8, "flash_move_max_pct": 3.0,
    "flash_move_window_sec": 45,
    "reversal_exit_score": 0.4, "reversal_exit_confidence": 0.6,
    "stale_exit_after_min": 60, "stale_exit_max_move_pct": 0.5,
    "adverse_tighten_start_rr_caution": 0.25,
    "range_trade": {"enabled": True, "edge_atr_mult": 0.5, "max_range_width_atr_mult": 4.0,
                    "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
}

RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["XUSDT"], "universe": {"enabled": False}},
    "risk": {
        "position_size_pct_of_equity": 40.0, "margin_buffer_pct": 0.0,
        "override_entry": {"enabled": False}, "max_leverage": 5,
        "leverage_by_symbol": {}, "default_leverage_range": {"min": 5, "max": 8},
        "max_daily_loss_pct": 8.0, "max_concurrent_positions": 4,
        "min_confidence_to_enter": 0.55, "min_order_notional_usdt": 5.0,
        "max_entry_chase_atr_mult": 3.0,
        "chase_caution_atr_mult": 1.5,
    },
    "signals": {
        "weights": {"technical": 0.4, "volume": 0.1, "news": 0.15, "polymarket": 0.2, "funding": 0.15},
        "technical": {"timeframes": ["15", "60", "240"], "atr_period": 14, "kline_limit": 200},
        "news": {"feeds": []}, "polymarket": {"keywords": []}, "funding": {"normalize_pct": 0.05},
    },
    "trade_management": TRADE_CFG,
    "loop": {"signal_refresh_sec": 30},
}


class FakeInst:
    qty_step = 0.01
    min_qty = 0.01
    tick_size = 0.01
    max_leverage = 25.0


def _make_strategy(tmp_path):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=RAW_CFG)
    state = StateStore(str(tmp_path / "state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    client.get_equity_usdt.return_value = 1000.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.get_last_price.return_value = 100.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(confidence=0.9, direction="long", extension=0.0):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": 1.0, "close": 100.0, "range_high": 0.0, "range_low": 0.0,
            "recent_extension_atr_mult": extension, "components": {}}


def test_long_blocked_when_chasing_a_recent_spike_up(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(direction="long", extension=4.0)  # > 3.0 threshold

    strategy.try_enter("XUSDT")

    client.open_position.assert_not_called()
    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "추격" in skip["reason"]


def test_short_blocked_when_chasing_a_recent_drop(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(direction="short", extension=-4.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_not_called()
    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "추격" in skip["reason"]


def test_long_allowed_when_extension_under_threshold(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(direction="long", extension=1.5)  # under 3.0

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()


def test_long_allowed_when_recent_move_was_actually_down(tmp_path):
    """A long candidate whose recent move was DOWN (not up) isn't chasing --
    that's a potential reversal entry, not exhaustion-chasing, so it's not
    blocked by this filter regardless of magnitude.
    """
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(direction="long", extension=-5.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()


def test_chase_filter_disabled_when_threshold_is_zero(tmp_path):
    cfg_disabled = {**RAW_CFG, "risk": {**RAW_CFG["risk"], "max_entry_chase_atr_mult": 0.0}}
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    strategy, state, client = _make_strategy(tmp_path)
    strategy.cfg = Config(secrets=secrets, raw=cfg_disabled)
    strategy.risk_cfg = strategy.cfg.get("risk", default={})
    strategy.get_signal = lambda s, force=False: _signal(direction="long", extension=10.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()


# -- chase_caution_atr_mult: can't predict a reversal, but can react faster if one happens -----------------------

def test_entry_flags_a_tighter_adverse_override_when_moderately_extended(tmp_path):
    """Below the hard block (3.0) but above the caution threshold (1.5) --
    the trade still opens, but carries its own tighter adverse-move-
    tightening trigger so a reversal on it is noticed and cut sooner.
    """
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(direction="long", extension=2.0)  # 1.5 < 2.0 < 3.0

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()
    trade = state.get_trade("XUSDT")
    assert trade["adverse_tighten_start_rr_override"] == 0.25


def test_entry_extension_recorded_in_the_decision_log_either_way(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(direction="long", extension=0.5)  # well under caution

    strategy.try_enter("XUSDT")

    trade = state.get_trade("XUSDT")
    assert "adverse_tighten_start_rr_override" not in trade


def test_short_flags_the_override_using_the_sign_adjusted_magnitude(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    # extension is negative (a down move); for a short candidate that's a
    # same-direction chase, so the sign-adjusted magnitude is +2.0
    strategy.get_signal = lambda s, force=False: _signal(direction="short", extension=-2.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()
    trade = state.get_trade("XUSDT")
    assert trade["adverse_tighten_start_rr_override"] == 0.25
