"""try_enter()/_open()/_enter_range() now record WHY a symbol was passed over
this tick (StateStore.set_skip_reason), surfaced on the dashboard's watchlist
card instead of a generic "waiting for a signal" -- answers the recurring
"지금 왜 거래가 없어?" question directly instead of needing a guess at which
of several possible gates (daily loss cap, confidence threshold, cooldown, no
margin room, ...) is currently active.
"""
import time
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
        "min_confidence_to_enter": 0.65, "min_order_notional_usdt": 5.0,
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
    client.close_position.return_value = {}
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(confidence=0.9, direction="long", atr=1.0, close=100.0):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": atr, "close": close, "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_records_reason_when_blocked_by_entry_backoff(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    state.set_entry_backoff("XUSDT", until_ts=time.time() + 300, reason="insufficient margin")
    strategy.get_signal = lambda s, force=False: _signal()

    strategy.try_enter("XUSDT")

    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "백오프" in skip["reason"]


def test_records_reason_when_confidence_too_low(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(confidence=0.5, direction="long")  # below 0.65

    strategy.try_enter("XUSDT")

    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "신뢰도" in skip["reason"]
    client.open_position.assert_not_called()


def test_records_reason_when_volatility_too_low(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    cfg_with_floor = {**RAW_CFG, "risk": {**RAW_CFG["risk"], "min_volatility_atr_pct": 5.0}}
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    strategy.cfg = Config(secrets=secrets, raw=cfg_with_floor)
    strategy.risk_cfg = strategy.cfg.get("risk", default={})
    strategy.get_signal = lambda s, force=False: _signal(atr=0.01, close=100.0)  # 0.01% << 5%

    strategy.try_enter("XUSDT")

    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "변동성" in skip["reason"]


def test_records_reason_when_stale_cooldown_active(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    state.set_stale_cooldown("XUSDT", "long", until_ts=time.time() + 600)
    strategy.get_signal = lambda s, force=False: _signal(direction="long")

    strategy.try_enter("XUSDT")

    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "쿨다운" in skip["reason"]


def test_records_reason_when_max_per_direction_reached(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.risk_cfg["max_concurrent_positions_per_direction"] = 1
    state.set_trade("AUSDT", {
        "symbol": "AUSDT", "side": "long", "entry_price": 100.0, "qty": 1.0,
        "initial_sl": 95.0, "initial_tp": 110.0, "current_sl": 95.0, "current_tp": 110.0,
        "risk_distance": 5.0, "entry_atr": 5.0, "breakeven_moved": False,
        "trailing_active": False, "tp_extensions_used": 0, "partial_tp_taken": False,
        "partial_realized_pnl": 0.0, "opened_at": 0.0,
    })
    strategy.get_signal = lambda s, force=False: _signal(direction="long")

    strategy.try_enter("XUSDT")

    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "한도" in skip["reason"]


def test_skip_reason_cleared_on_successful_entry(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    state.set_skip_reason("XUSDT", "신뢰도 부족 (stale from an earlier tick)")
    strategy.get_signal = lambda s, force=False: _signal(confidence=0.9, direction="long")
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()
    assert state.get_skip_reason("XUSDT") is None
