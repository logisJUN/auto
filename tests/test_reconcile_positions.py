"""Strategy._reconcile_orphaned_positions(): a position can be open on the
exchange with no corresponding entry in local state -- e.g. state.json was
reset (a real risk on Render's free plan) or a position was opened outside the
bot. Without this, tick() would never even look at it, since it only manages
symbols in the watchlist or already-tracked state. Covers adoption with an
existing SL/TP, adoption after a successful repair, closing on a failed
repair, and leaving already-tracked positions alone.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitAPIError, BybitClient
from bot.notify import Notifier
from bot.risk import stop_manager
from bot.state import StateStore
from bot.strategy import Strategy

RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["PINNEDUSDT"], "universe": {"enabled": False}},
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
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.close_position.return_value = {}
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _orphan(symbol, stop_loss=0, take_profit=0):
    return {"symbol": symbol, "side": "Buy", "size": 2.0, "entry_price": 50.0,
            "unrealized_pnl": 0.0, "position_idx": 0,
            "stop_loss": stop_loss, "take_profit": take_profit}


def test_adopts_orphan_keeping_its_existing_sl_tp(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: {"atr": 1.0}
    client.get_all_open_positions.return_value = [_orphan("ORPHANUSDT", stop_loss=45.0, take_profit=60.0)]

    strategy._reconcile_orphaned_positions()

    trade = state.get_trade("ORPHANUSDT")
    assert trade is not None
    assert trade["current_sl"] == 45.0
    assert trade["current_tp"] == 60.0
    client.update_trading_stop.assert_not_called()
    client.close_position.assert_not_called()


def test_adopts_orphan_after_repairing_missing_sl_tp(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: {"atr": 1.0}
    client.get_all_open_positions.return_value = [_orphan("ORPHANUSDT")]
    client.update_trading_stop.return_value = {}

    strategy._reconcile_orphaned_positions()

    client.update_trading_stop.assert_called_once()
    assert state.get_trade("ORPHANUSDT") is not None
    client.close_position.assert_not_called()


def test_closes_orphan_when_repair_fails(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: {"atr": 1.0}
    client.get_all_open_positions.return_value = [_orphan("ORPHANUSDT")]
    client.update_trading_stop.side_effect = BybitAPIError("boom")

    strategy._reconcile_orphaned_positions()

    client.close_position.assert_called_once()
    assert state.get_trade("ORPHANUSDT") is None


def test_already_tracked_position_is_left_alone(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    existing = stop_manager.new_trade("PINNEDUSDT", "long", entry_price=100.0, qty=1.0, atr=1.0,
                                       cfg=RAW_CFG["trade_management"])
    state.set_trade("PINNEDUSDT", existing)
    client.get_all_open_positions.return_value = [_orphan("PINNEDUSDT", stop_loss=45.0, take_profit=60.0)]

    strategy._reconcile_orphaned_positions()

    # reconcile must not touch it -- manage_open_position's own per-tick check
    # (already covered in test_sl_tp_protection.py) owns tracked positions.
    client.update_trading_stop.assert_not_called()
    client.close_position.assert_not_called()
    assert state.get_trade("PINNEDUSDT")["current_sl"] == existing["current_sl"]
