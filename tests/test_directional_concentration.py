"""risk.max_concurrent_positions_per_direction and
risk.same_direction_confidence_step: guard against the whole watchlist piling
into the same side. Observed live: BTCUSDT/ETHUSDT both stuck short through a
sustained uptrend, each new signal confidently (but wrongly) re-confirming
the same directional bet with no cap on how much of the portfolio could point
one way.
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
    "range_trade": {"enabled": True, "edge_atr_mult": 0.5, "max_range_width_atr_mult": 4.0,
                    "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
}


def _raw_cfg(**risk_overrides):
    risk = {
        "position_size_pct_of_equity": 45.0, "margin_buffer_pct": 10.0,
        "override_entry": {"enabled": False}, "max_leverage": 5,
        "leverage_by_symbol": {}, "default_leverage_range": {"min": 5, "max": 8},
        "max_daily_loss_pct": 8.0, "max_concurrent_positions": 4,
        "min_confidence_to_enter": 0.5, "min_order_notional_usdt": 5.0,
    }
    risk.update(risk_overrides)
    return {
        "exchange": {"category": "linear", "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                     "universe": {"enabled": False}},
        "risk": risk,
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


def _make_strategy(tmp_path, **risk_overrides):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=_raw_cfg(**risk_overrides))
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
        "symbol": "X", "side": "Sell", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 105.0, "take_profit": 90.0,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _open_short(state, symbol, entry_price=100.0):
    trade = stop_manager.new_trade(symbol, "short", entry_price=entry_price, qty=1.0, atr=1.0, cfg=TRADE_CFG)
    state.set_trade(symbol, trade)


def _signal(confidence, direction="short"):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": 1.0, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


# -- max_concurrent_positions_per_direction ---------------------------------------------------------

def test_entry_blocked_at_max_same_direction_positions(tmp_path):
    strategy, state, client = _make_strategy(tmp_path, max_concurrent_positions_per_direction=2)
    _open_short(state, "BTCUSDT")
    _open_short(state, "ETHUSDT")
    strategy.get_signal = lambda s, force=False: _signal(0.9, "short")

    strategy.try_enter("SOLUSDT")

    client.open_position.assert_not_called()
    assert state.get_trade("SOLUSDT") is None


def test_entry_allowed_when_under_the_per_direction_cap(tmp_path):
    strategy, state, client = _make_strategy(tmp_path, max_concurrent_positions_per_direction=2)
    _open_short(state, "BTCUSDT")
    strategy.get_signal = lambda s, force=False: _signal(0.9, "short")

    strategy.try_enter("ETHUSDT")

    client.open_position.assert_called_once()


def test_opposite_direction_not_blocked_by_same_direction_cap(tmp_path):
    strategy, state, client = _make_strategy(tmp_path, max_concurrent_positions_per_direction=2)
    _open_short(state, "BTCUSDT")
    _open_short(state, "ETHUSDT")
    strategy.get_signal = lambda s, force=False: _signal(0.9, "long")

    strategy.try_enter("SOLUSDT")

    client.open_position.assert_called_once()


def test_per_direction_cap_disabled_when_zero(tmp_path):
    strategy, state, client = _make_strategy(tmp_path, max_concurrent_positions_per_direction=0)
    _open_short(state, "BTCUSDT")
    _open_short(state, "ETHUSDT")
    strategy.get_signal = lambda s, force=False: _signal(0.9, "short")

    strategy.try_enter("SOLUSDT")

    client.open_position.assert_called_once()


# -- same_direction_confidence_step ---------------------------------------------------------

def test_confidence_bar_rises_with_existing_same_direction_positions(tmp_path):
    strategy, state, client = _make_strategy(
        tmp_path, min_confidence_to_enter=0.5, same_direction_confidence_step=0.1)
    _open_short(state, "BTCUSDT")  # one short already open -> effective bar is 0.5+0.1=0.6
    strategy.get_signal = lambda s, force=False: _signal(0.55, "short")  # clears base 0.5, not 0.6

    strategy.try_enter("ETHUSDT")

    client.open_position.assert_not_called()


def test_confidence_bar_unaffected_when_step_is_zero(tmp_path):
    strategy, state, client = _make_strategy(
        tmp_path, min_confidence_to_enter=0.5, same_direction_confidence_step=0.0)
    _open_short(state, "BTCUSDT")
    strategy.get_signal = lambda s, force=False: _signal(0.55, "short")

    strategy.try_enter("ETHUSDT")

    client.open_position.assert_called_once()


def test_confidence_bar_only_rises_for_the_skewed_side(tmp_path):
    strategy, state, client = _make_strategy(
        tmp_path, min_confidence_to_enter=0.5, same_direction_confidence_step=0.1)
    _open_short(state, "BTCUSDT")  # only shorts are skewed
    strategy.get_signal = lambda s, force=False: _signal(0.55, "long")  # base bar applies, not raised

    strategy.try_enter("ETHUSDT")

    client.open_position.assert_called_once()
