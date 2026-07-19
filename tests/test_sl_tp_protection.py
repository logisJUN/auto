"""Bybit's place_order stopLoss/takeProfit params can silently fail to attach
even when the base market order fills, leaving a naked leveraged position with
no protective stop. Covers the verify-and-repair-or-close-immediately logic in
Strategy._open() (at entry) and Strategy.manage_open_position() (ongoing, every
tick) that guards against this.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.risk import stop_manager
from bot.state import StateStore
from bot.strategy import Strategy


class FakeInst:
    qty_step = 0.01
    min_qty = 0.01
    tick_size = 0.01
    max_leverage = 25.0


RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["XUSDT"], "universe": {"enabled": False}},
    "risk": {
        "position_size_pct_of_equity": 50.0,
        "margin_buffer_pct": 15.0,
        "override_entry": {"enabled": False},
        "max_leverage": 5,
        "leverage_by_symbol": {},
        "default_leverage_range": {"min": 5, "max": 8},
        "max_daily_loss_pct": 8.0,
        "max_concurrent_positions": 4,
        "min_confidence_to_enter": 0.55,
        "min_order_notional_usdt": 5.0,
    },
    "signals": {
        "weights": {"technical": 0.25, "volume": 0.15, "news": 0.2, "polymarket": 0.4},
        "technical": {"timeframes": ["15", "60", "240"], "atr_period": 14, "kline_limit": 200},
        "news": {"feeds": []},
        "polymarket": {"keywords": []},
    },
    "trade_management": {
        "atr_sl_multiplier": 1.5, "atr_tp_multiplier": 2.5,
        "breakeven_after_rr": 0.5, "trail_activation_rr": 1.0, "trail_atr_multiplier": 1.2,
        "tp_extend_atr_step": 0.75, "max_tp_extensions": 3,
        "flash_move_atr_mult": 1.0, "flash_move_min_pct": 0.8, "flash_move_max_pct": 3.0,
        "flash_move_window_sec": 45,
        "reversal_exit_score": 0.4, "reversal_exit_confidence": 0.6,
        "stale_exit_after_min": 60, "stale_exit_max_move_pct": 0.5,
        "range_trade": {"enabled": True, "edge_atr_mult": 0.5,
                        "max_range_width_atr_mult": 4.0,
                        "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
    },
    "loop": {"signal_refresh_sec": 120},
}


def _make_strategy(tmp_path):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=RAW_CFG)
    state = StateStore(str(tmp_path / "state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    client.get_equity_usdt.return_value = 1000.0
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.get_last_price.return_value = 100.0
    client.open_position.return_value = {}
    client.close_position.return_value = {}
    client.get_closed_pnl.return_value = {
        "closed_pnl": 1.0, "avg_exit_price": 100.0, "updated_time_ms": 99999999999999,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(confidence=0.6):
    return {"score": confidence, "confidence": confidence, "direction": "long",
            "atr": 1.0, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


def _position(stop_loss, take_profit):
    return {"symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
            "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": stop_loss, "take_profit": take_profit}


# -- at entry ---------------------------------------------------------

def test_entry_records_trade_normally_when_sl_tp_actually_attached(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_position.return_value = _position(stop_loss=85.0, take_profit=115.0)

    strategy.try_enter("XUSDT")

    assert state.get_trade("XUSDT") is not None
    client.update_trading_stop.assert_not_called()
    client.close_position.assert_not_called()


def test_entry_repairs_missing_sl_tp_and_still_records_trade(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_position.return_value = _position(stop_loss=0, take_profit=0)  # attach silently failed
    client.update_trading_stop.return_value = {}  # repair succeeds

    strategy.try_enter("XUSDT")

    client.update_trading_stop.assert_called_once()
    assert state.get_trade("XUSDT") is not None
    client.close_position.assert_not_called()


def test_entry_closes_immediately_when_sl_tp_missing_and_repair_fails(tmp_path):
    from bot.exchange.bybit_client import BybitAPIError

    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_position.return_value = _position(stop_loss=0, take_profit=0)
    client.update_trading_stop.side_effect = BybitAPIError("boom")

    strategy.try_enter("XUSDT")

    client.close_position.assert_called_once()
    assert state.get_trade("XUSDT") is None  # never recorded as a tracked, "protected" position


# -- ongoing, every tick ---------------------------------------------------------

def test_manage_open_position_repairs_missing_sl_tp_without_closing(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = stop_manager.new_trade("XUSDT", "long", entry_price=100.0, qty=1.0, atr=1.0,
                                    cfg=RAW_CFG["trade_management"])
    state.set_trade("XUSDT", trade)
    strategy.get_signal = lambda s, force=False: _signal()

    client.get_position.return_value = _position(stop_loss=0, take_profit=0)  # missing
    client.update_trading_stop.return_value = {}  # repair succeeds

    strategy.manage_open_position("XUSDT")

    client.update_trading_stop.assert_called_once()
    assert state.get_trade("XUSDT") is not None  # kept open, not closed
    client.close_position.assert_not_called()


def test_manage_open_position_closes_when_sl_tp_missing_and_repair_fails(tmp_path):
    from bot.exchange.bybit_client import BybitAPIError

    strategy, state, client = _make_strategy(tmp_path)
    trade = stop_manager.new_trade("XUSDT", "long", entry_price=100.0, qty=1.0, atr=1.0,
                                    cfg=RAW_CFG["trade_management"])
    state.set_trade("XUSDT", trade)

    client.get_position.return_value = _position(stop_loss=0, take_profit=0)
    client.update_trading_stop.side_effect = BybitAPIError("boom")

    strategy.manage_open_position("XUSDT")

    client.close_position.assert_called_once()
    assert state.get_trade("XUSDT") is None
