"""Strategy._margin_for_new_position also caps by the exchange's own real
available balance (BybitClient.get_available_balance_usdt), not just the
locally-computed headroom estimate -- our local estimate (used_margin summed
from state) can drift from what Bybit actually allows (funding fees, price
moves since our last snapshot), which showed up live as repeated ErrCode
110007 ("insufficient margin") rejections even with a margin buffer.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.state import StateStore
from bot.strategy import Strategy

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
    "trade_management": {
        "atr_sl_multiplier": 1.5, "atr_tp_multiplier": 2.5,
        "breakeven_after_rr": 0.5, "trail_activation_rr": 1.0, "trail_atr_multiplier": 1.2,
        "tp_extend_atr_step": 0.75, "max_tp_extensions": 3,
        "flash_move_atr_mult": 1.0, "flash_move_min_pct": 0.8, "flash_move_max_pct": 3.0,
        "flash_move_window_sec": 45,
        "reversal_exit_score": 0.4, "reversal_exit_confidence": 0.6,
        "stale_exit_after_min": 60, "stale_exit_max_move_pct": 0.5,
        "range_trade": {"enabled": True, "edge_atr_mult": 0.5, "max_range_width_atr_mult": 4.0,
                        "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
    },
    "loop": {"signal_refresh_sec": 30},
}


def _make_strategy(tmp_path):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=RAW_CFG)
    state = StateStore(str(tmp_path / "state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def test_margin_capped_by_exchange_available_balance(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    # local calc would target 50% of 1000 = 500, but the exchange says only 80 is really free
    client.get_available_balance_usdt.return_value = 80.0

    margin = strategy._margin_for_new_position(equity=1000.0)

    assert margin == 80.0


def test_margin_unaffected_when_available_balance_exceeds_local_target(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    client.get_available_balance_usdt.return_value = 1_000_000.0

    margin = strategy._margin_for_new_position(equity=1000.0)

    assert margin == 500.0  # local target (50% of equity) still binds


def test_margin_falls_back_to_local_calc_when_available_balance_unknown(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    client.get_available_balance_usdt.return_value = None

    margin = strategy._margin_for_new_position(equity=1000.0)

    assert margin == 500.0


def test_available_balance_lookup_is_cached_briefly(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    client.get_available_balance_usdt.return_value = 80.0

    strategy._margin_for_new_position(equity=1000.0)
    strategy._margin_for_new_position(equity=1000.0)
    strategy._margin_for_new_position(equity=1000.0)

    assert client.get_available_balance_usdt.call_count == 1
