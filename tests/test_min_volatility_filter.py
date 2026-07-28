"""risk.min_volatility_atr_pct: skip entries (trend or range) when the
execution-timeframe ATR is too small relative to price -- a dead/chopping
market where the likely move doesn't clearly clear the round-trip taker fee,
the exact failure mode diagnosed from a real SUIUSDT loss that matched its
fee exactly.
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
        "min_volatility_atr_pct": 0.15,
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


class FakeInst:
    qty_step = 0.1
    min_qty = 0.1
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
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 1)
    client.get_last_price.return_value = 100.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "X", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(atr, confidence=0.9, direction="long"):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": atr, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_entry_skipped_when_atr_pct_below_floor(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(atr=0.05)  # 0.05% of 100 close

    strategy.try_enter("XUSDT")

    assert client.open_position.called is False
    assert state.get_trade("XUSDT") is None


def test_entry_allowed_when_atr_pct_at_or_above_floor(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(atr=0.5)  # 0.5% of 100 close

    strategy.try_enter("XUSDT")

    assert client.open_position.called is True
    assert state.get_trade("XUSDT") is not None


def test_filter_disabled_when_min_atr_pct_is_zero(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.risk_cfg["min_volatility_atr_pct"] = 0.0
    strategy.get_signal = lambda s, force=False: _signal(atr=0.001)

    strategy.try_enter("XUSDT")

    assert client.open_position.called is True
