"""risk.symbol_pause_after_consecutive_losses: auto-pauses a symbol whose
signal keeps being wrong specifically for it, independent of the account-wide
daily loss circuit breaker. Observed live: SNXXUSDT went 0-for-5 (every
single close a loss) while other symbols traded fine on the same signal
logic over the same period. Reuses the existing entry-backoff mechanism, so
a paused symbol shows up in the dashboard's skip-reason column like any
other backoff.
"""
import time
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

RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["XUSDT"], "universe": {"enabled": False}},
    "risk": {
        "position_size_pct_of_equity": 50.0, "margin_buffer_pct": 0.0,
        "override_entry": {"enabled": False}, "max_leverage": 5,
        "leverage_by_symbol": {}, "default_leverage_range": {"min": 5, "max": 8},
        "max_daily_loss_pct": 8.0, "max_concurrent_positions": 4,
        "min_confidence_to_enter": 0.55, "min_order_notional_usdt": 5.0,
        "symbol_pause_after_consecutive_losses": 3, "symbol_pause_duration_min": 720,
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
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.get_last_price.return_value = 100.0
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _losing_trade(side="long"):
    return stop_manager.new_trade("XUSDT", side, entry_price=100.0, qty=1.0, atr=1.0, cfg=TRADE_CFG)


def _close_losing(strategy, client, symbol="XUSDT"):
    trade = _losing_trade()
    client.get_closed_pnl.return_value = {
        "closed_pnl": -1.0, "avg_exit_price": 95.0, "updated_time_ms": 99999999999999,
    }
    strategy._close_and_settle(symbol, trade, "sl_tp_hit", already_closed=True)


def _close_winning(strategy, client, symbol="XUSDT"):
    trade = _losing_trade()
    client.get_closed_pnl.return_value = {
        "closed_pnl": 1.0, "avg_exit_price": 105.0, "updated_time_ms": 99999999999999,
    }
    strategy._close_and_settle(symbol, trade, "sl_tp_hit", already_closed=True)


def test_symbol_paused_after_threshold_consecutive_losses(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)

    _close_losing(strategy, client)
    _close_losing(strategy, client)
    assert state.get_entry_backoff("XUSDT") is None  # only 2 losses so far, threshold is 3

    _close_losing(strategy, client)  # 3rd consecutive loss trips it

    backoff = state.get_entry_backoff("XUSDT")
    assert backoff is not None
    assert "3연속 손실" in backoff["reason"]
    remaining_min = (backoff["until_ts"] - time.time()) / 60
    assert 719 < remaining_min <= 720


def test_a_win_resets_the_streak_and_avoids_the_pause(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)

    _close_losing(strategy, client)
    _close_losing(strategy, client)
    _close_winning(strategy, client)
    _close_losing(strategy, client)
    _close_losing(strategy, client)

    # streak reset by the win, so only 2 losses in a row since -- not paused
    assert state.get_entry_backoff("XUSDT") is None
    assert state.get_consecutive_losses("XUSDT") == 2


def test_pause_blocks_a_subsequent_entry_via_existing_backoff_check(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    for _ in range(3):
        _close_losing(strategy, client)

    strategy.get_signal = lambda s, force=False: {
        "score": 0.9, "confidence": 0.9, "direction": "long", "atr": 1.0, "close": 100.0,
        "range_high": 0.0, "range_low": 0.0, "components": {},
    }

    strategy.try_enter("XUSDT")

    client.open_position.assert_not_called()
    skip = state.get_skip_reason("XUSDT")
    assert skip is not None
    assert "3연속 손실" in skip["reason"]


def test_tracked_independently_per_symbol(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    for _ in range(3):
        _close_losing(strategy, client, symbol="AUSDT")
    _close_losing(strategy, client, symbol="BUSDT")

    assert state.get_entry_backoff("AUSDT") is not None
    assert state.get_entry_backoff("BUSDT") is None


def test_disabled_when_threshold_is_zero(tmp_path):
    cfg_disabled = {**TRADE_CFG}
    strategy, state, client = _make_strategy(tmp_path, trade_cfg=cfg_disabled)
    strategy.risk_cfg["symbol_pause_after_consecutive_losses"] = 0

    for _ in range(5):
        _close_losing(strategy, client)

    assert state.get_entry_backoff("XUSDT") is None
    # the streak itself is still tracked even while the pause is disabled
    assert state.get_consecutive_losses("XUSDT") == 5
