"""Covers the override-reallocation path: when there's no slot/margin room left,
a strong enough new trend signal should close the weakest currently-held
position and take that trade instead of sitting out.
"""
from unittest.mock import MagicMock

import yaml

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


def _make_strategy(state_path, log_path):
    with open("config.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=raw)
    state = StateStore(state_path)
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    client.get_equity_usdt.return_value = 1000.0
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
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

    for sym in strategy.symbols[:2]:
        signals[sym] = _signal(0.6)
        strategy.try_enter(sym)
    assert state.open_trade_count() == 2  # 50% + 35% margin -> headroom exhausted

    weakest_symbol = strategy.symbols[1]
    signals[weakest_symbol] = _signal(0.1, direction="neutral")  # its thesis has faded

    strategy.symbols.append("NEWUSDT")
    signals["NEWUSDT"] = _signal(0.95)
    strategy.try_enter("NEWUSDT")

    assert state.get_trade(weakest_symbol) is None
    assert state.get_trade("NEWUSDT") is not None


def test_weak_new_signal_does_not_displace_anything(tmp_path):
    strategy, state = _make_strategy(str(tmp_path / "state.json"), str(tmp_path / "logs"))
    signals = {}
    strategy.get_signal = lambda s, force=False: signals[s]

    for sym in strategy.symbols[:2]:
        signals[sym] = _signal(0.6)
        strategy.try_enter(sym)
    assert state.open_trade_count() == 2

    strategy.symbols.append("NEWUSDT")
    signals["NEWUSDT"] = _signal(0.7)  # above min_confidence_to_enter but below override threshold (0.85)
    strategy.try_enter("NEWUSDT")

    assert state.get_trade("NEWUSDT") is None
    assert state.open_trade_count() == 2  # nothing displaced


def test_override_disabled_never_displaces(tmp_path):
    strategy, state = _make_strategy(str(tmp_path / "state.json"), str(tmp_path / "logs"))
    strategy.risk_cfg["override_entry"] = {**strategy.risk_cfg["override_entry"], "enabled": False}
    signals = {}
    strategy.get_signal = lambda s, force=False: signals[s]

    for sym in strategy.symbols[:2]:
        signals[sym] = _signal(0.6)
        strategy.try_enter(sym)

    strategy.symbols.append("NEWUSDT")
    signals["NEWUSDT"] = _signal(0.99)  # would easily qualify if enabled
    strategy.try_enter("NEWUSDT")

    assert state.get_trade("NEWUSDT") is None
    assert state.open_trade_count() == 2
