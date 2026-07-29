"""trade_management.stale_reentry_cooldown_min: after a stale_timeout close,
block a same-symbol/same-direction re-entry for a while -- observed live as a
symbol stuck in a tight range getting shorted, stale-timed-out, and
immediately re-shorted in a fee-bleeding loop. A genuine reversal (signal now
favors the other side) must not be blocked.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.risk import stop_manager
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
    "stale_reentry_cooldown_min": 30,
    "range_trade": {"enabled": True, "edge_atr_mult": 0.5, "max_range_width_atr_mult": 4.0,
                    "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
}

RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["XUSDT"], "universe": {"enabled": False}},
    "risk": {
        "position_size_pct_of_equity": 50.0, "margin_buffer_pct": 0.0,
        "override_entry": {"enabled": False}, "max_leverage": 5,
        "leverage_by_symbol": {}, "default_leverage_range": {"min": 5, "max": 8},
        "max_daily_loss_pct": 8.0, "max_concurrent_positions": 4,
        "min_confidence_to_enter": 0.55, "min_order_notional_usdt": 5.0,
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


def _make_strategy(tmp_path, trade_cfg=TRADE_CFG):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    raw = {**RAW_CFG, "trade_management": trade_cfg}
    cfg = Config(secrets=secrets, raw=raw)
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
        "symbol": "XUSDT", "side": "Sell", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 105.0, "take_profit": 90.0,
    }
    client.get_closed_pnl.return_value = {
        "closed_pnl": -0.5, "avg_exit_price": 100.0, "updated_time_ms": 99999999999999,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(confidence=0.9, direction="short"):
    return {"score": -confidence if direction == "short" else confidence, "confidence": confidence,
            "direction": direction, "atr": 1.0, "close": 100.0,
            "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_stale_close_records_a_cooldown(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = stop_manager.new_trade("XUSDT", "short", entry_price=100.0, qty=1.0, atr=1.0, cfg=TRADE_CFG)

    strategy._close_and_settle("XUSDT", trade, "stale_timeout", already_closed=True)

    cooldown = state.get_stale_cooldown("XUSDT")
    assert cooldown is not None
    assert cooldown["side"] == "short"


def test_same_direction_reentry_blocked_during_cooldown(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    import time
    state.set_stale_cooldown("XUSDT", "short", until_ts=time.time() + 60)
    strategy.get_signal = lambda s, force=False: _signal(direction="short")

    strategy.try_enter("XUSDT")

    client.open_position.assert_not_called()
    assert state.get_trade("XUSDT") is None


def test_opposite_direction_reentry_not_blocked_during_cooldown(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    import time
    state.set_stale_cooldown("XUSDT", "short", until_ts=time.time() + 60)
    strategy.get_signal = lambda s, force=False: _signal(direction="long")

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()
    assert state.get_trade("XUSDT") is not None


def test_reentry_allowed_once_cooldown_expires(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    import time
    state.set_stale_cooldown("XUSDT", "short", until_ts=time.time() - 1)  # already expired
    strategy.get_signal = lambda s, force=False: _signal(direction="short")

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()
    assert state.get_trade("XUSDT") is not None


def test_cooldown_disabled_when_config_value_is_zero(tmp_path):
    cfg_no_cooldown = {**TRADE_CFG, "stale_reentry_cooldown_min": 0}
    strategy, state, client = _make_strategy(tmp_path, trade_cfg=cfg_no_cooldown)
    trade = stop_manager.new_trade("XUSDT", "short", entry_price=100.0, qty=1.0, atr=1.0, cfg=cfg_no_cooldown)

    strategy._close_and_settle("XUSDT", trade, "stale_timeout", already_closed=True)

    assert state.get_stale_cooldown("XUSDT") is None


def test_other_close_reasons_do_not_record_a_cooldown(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = stop_manager.new_trade("XUSDT", "short", entry_price=100.0, qty=1.0, atr=1.0, cfg=TRADE_CFG)

    strategy._close_and_settle("XUSDT", trade, "sl_tp_hit", already_closed=True)

    assert state.get_stale_cooldown("XUSDT") is None
