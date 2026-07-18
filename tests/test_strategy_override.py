"""Covers the override-reallocation path: when there's no slot/margin room left,
a strong enough new trend signal should close the weakest currently-held
position and take that trade instead of sitting out.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.state import StateStore
from bot.strategy import Strategy


class FakeInst:
    qty_step = 0.01
    min_qty = 0.01
    tick_size = 0.01
    max_leverage = 25.0


# Self-contained minimal config -- deliberately NOT loaded from the real
# config.yaml, so edits to the live symbol list/universe settings there can't
# silently break these tests (they exercise the override mechanism itself,
# not the dynamic universe scan).
RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["ETHUSDT", "SOXLUSDT"],
                 "universe": {"enabled": False}},
    "risk": {
        "position_size_pct_of_equity": 50.0,
        "margin_buffer_pct": 15.0,
        "override_entry": {
            "enabled": True,
            "min_confidence": 0.85,
            "min_confidence_margin_over_weakest": 0.15,
        },
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
        "flash_move_pct": 1.2, "flash_move_window_sec": 45,
        "reversal_exit_score": 0.4, "reversal_exit_confidence": 0.6,
        "stale_exit_after_min": 60, "stale_exit_max_move_pct": 0.5,
        "range_trade": {"enabled": True, "edge_atr_mult": 0.5,
                        "max_range_width_atr_mult": 4.0,
                        "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
    },
    "loop": {"signal_refresh_sec": 120},
}


def _make_strategy(state_path, log_path):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=RAW_CFG)
    state = StateStore(state_path)
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    client.get_equity_usdt.return_value = 1000.0
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.get_last_price.return_value = 100.0
    client.open_position.return_value = {}
    client.get_position.return_value = None
    client.close_position.return_value = {}
    client.get_closed_pnl.return_value = {
        "closed_pnl": 1.0, "avg_exit_price": 100.0, "updated_time_ms": 99999999999999,
    }
    return Strategy(client, cfg, state, notifier, log_path), state


def _signal(confidence, direction="long"):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": 1.0, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_strong_new_signal_displaces_weakest_position_when_no_margin_room(tmp_path):
    strategy, state = _make_strategy(str(tmp_path / "state.json"), str(tmp_path / "logs"))
    signals = {}
    strategy.get_signal = lambda s, force=False: signals[s]

    for sym in strategy.symbols:  # ["ETHUSDT", "SOXLUSDT"]
        signals[sym] = _signal(0.6)
        strategy.try_enter(sym)
    assert state.open_trade_count() == 2  # 50% + 35% margin -> headroom exhausted

    weakest_symbol = strategy.symbols[1]
    signals[weakest_symbol] = _signal(0.1, direction="neutral")  # its thesis has faded

    strategy.symbols = strategy.symbols + ["NEWUSDT"]
    signals["NEWUSDT"] = _signal(0.95)
    strategy.try_enter("NEWUSDT")

    assert state.get_trade(weakest_symbol) is None
    assert state.get_trade("NEWUSDT") is not None


def test_weak_new_signal_does_not_displace_anything(tmp_path):
    strategy, state = _make_strategy(str(tmp_path / "state.json"), str(tmp_path / "logs"))
    signals = {}
    strategy.get_signal = lambda s, force=False: signals[s]

    for sym in strategy.symbols:
        signals[sym] = _signal(0.6)
        strategy.try_enter(sym)
    assert state.open_trade_count() == 2

    strategy.symbols = strategy.symbols + ["NEWUSDT"]
    signals["NEWUSDT"] = _signal(0.7)  # above min_confidence_to_enter but below override threshold (0.85)
    strategy.try_enter("NEWUSDT")

    assert state.get_trade("NEWUSDT") is None
    assert state.open_trade_count() == 2  # nothing displaced


def test_override_disabled_never_displaces(tmp_path):
    strategy, state = _make_strategy(str(tmp_path / "state.json"), str(tmp_path / "logs"))
    strategy.risk_cfg["override_entry"] = {**strategy.risk_cfg["override_entry"], "enabled": False}
    signals = {}
    strategy.get_signal = lambda s, force=False: signals[s]

    for sym in strategy.symbols:
        signals[sym] = _signal(0.6)
        strategy.try_enter(sym)

    strategy.symbols = strategy.symbols + ["NEWUSDT"]
    signals["NEWUSDT"] = _signal(0.99)  # would easily qualify if enabled
    strategy.try_enter("NEWUSDT")

    assert state.get_trade("NEWUSDT") is None
    assert state.open_trade_count() == 2


def test_unlisted_symbol_uses_default_leverage_range(tmp_path):
    strategy, _ = _make_strategy(str(tmp_path / "state.json"), str(tmp_path / "logs"))
    inst = FakeInst()
    # ETHUSDT isn't in leverage_by_symbol in this test config either, so both
    # should fall back to default_leverage_range (5-8).
    lev = strategy._leverage_for("SOMENEWCOINUSDT", inst, confidence=1.0)
    assert lev == 8  # default_leverage_range max
    lev_min_conf = strategy._leverage_for("SOMENEWCOINUSDT", inst, confidence=0.0)
    assert lev_min_conf == 5  # default_leverage_range min
