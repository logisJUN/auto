"""trade_management.partial_tp: once an open trade reaches at_rr multiples of
its initial risk, close close_fraction of the position at market to lock in
profit, and keep managing the remainder as usual (still eligible for
breakeven/trailing/TP-extension). Range/scalp trades are excluded (see
manage_open_position). Skipped for a trade too small to split without
leaving an unclosable dust remainder.
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
    "partial_tp": {"enabled": True, "at_rr": 1.0, "close_fraction": 0.5},
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
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.close_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 10.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 85.0, "take_profit": 130.0,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _open_trade(qty=10.0):
    # risk_distance = atr(1.0) * atr_sl_multiplier(1.5) = 1.5 -> 1R price move = 1.5
    return stop_manager.new_trade("XUSDT", "long", entry_price=100.0, qty=qty, atr=1.0, cfg=TRADE_CFG)


def _signal(price=100.0):
    return {"score": 0.0, "confidence": 0.0, "direction": "neutral",
            "atr": 1.0, "close": price, "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_partial_close_fires_once_price_reaches_1r(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(qty=10.0)
    state.set_trade("XUSDT", trade)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_last_price.return_value = 102.0  # entry 100 + 1.5(R)*1 = 101.5 -> past 1R
    client.get_closed_pnl.return_value = {
        "closed_pnl": 10.0, "avg_exit_price": 102.0, "updated_time_ms": 99999999999999,
    }

    strategy.manage_open_position("XUSDT")

    client.close_position.assert_called_once_with("XUSDT", "long", 5.0)
    trade = state.get_trade("XUSDT")
    assert trade is not None, "remainder stays open, not fully closed"
    assert trade["qty"] == 5.0
    assert trade["partial_tp_taken"] is True
    assert trade["partial_realized_pnl"] == 10.0
    assert state.snapshot()["daily"]["realized_pnl"] == 10.0


def test_partial_close_does_not_refire_on_next_tick(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(qty=10.0)
    trade["partial_tp_taken"] = True  # already taken on a previous tick
    trade["partial_realized_pnl"] = 10.0
    trade["qty"] = 5.0
    state.set_trade("XUSDT", trade)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_last_price.return_value = 103.0

    strategy.manage_open_position("XUSDT")

    client.close_position.assert_not_called()


def test_partial_close_skipped_when_it_would_leave_unclosable_dust(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(qty=0.01)  # smallest possible size -- can't be split further
    state.set_trade("XUSDT", trade)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_last_price.return_value = 102.0

    strategy.manage_open_position("XUSDT")

    client.close_position.assert_not_called()
    trade = state.get_trade("XUSDT")
    assert trade["qty"] == 0.01
    assert trade["partial_tp_taken"] is False


def test_final_close_reports_total_pnl_including_earlier_partial_leg(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(qty=5.0)
    trade["partial_tp_taken"] = True
    trade["partial_realized_pnl"] = 10.0  # booked earlier when the partial fired
    state.set_trade("XUSDT", trade)
    client.get_closed_pnl.return_value = {
        "closed_pnl": 4.0, "avg_exit_price": 108.0, "updated_time_ms": 99999999999999,
    }

    strategy._close_and_settle("XUSDT", trade, "sl_tp_hit", already_closed=True)

    closed_trades = state.snapshot()["history"]
    assert closed_trades[-1]["pnl"] == 14.0  # 10 (partial) + 4 (final leg)
    # only the final leg's pnl gets freshly added -- the partial's 10 was
    # already booked into daily realized_pnl when it happened.
    assert state.snapshot()["daily"]["realized_pnl"] == 4.0


def test_partial_tp_disabled_when_not_configured(tmp_path):
    cfg_without_partial = {k: v for k, v in TRADE_CFG.items() if k != "partial_tp"}
    strategy, state, client = _make_strategy(tmp_path, trade_cfg=cfg_without_partial)
    trade = _open_trade(qty=10.0)
    state.set_trade("XUSDT", trade)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_last_price.return_value = 102.0

    strategy.manage_open_position("XUSDT")

    client.close_position.assert_not_called()
    assert state.get_trade("XUSDT")["qty"] == 10.0


def test_partial_tp_not_applied_to_range_trades(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(qty=10.0)
    trade["is_range_trade"] = True
    state.set_trade("XUSDT", trade)
    strategy.get_signal = lambda s, force=False: _signal()
    client.get_last_price.return_value = 102.0

    strategy.manage_open_position("XUSDT")

    client.close_position.assert_not_called()
    assert state.get_trade("XUSDT")["qty"] == 10.0
