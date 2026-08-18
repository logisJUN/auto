"""risk.max_position_risk_pct: skip an entry entirely if a full SL hit on it
alone would cost more than this % of current equity -- (margin/equity) *
leverage * sl_distance_pct. Catches the case margin/leverage sizing caps
alone don't: a wide ATR-based SL distance on a volatile symbol can blow past
any reasonable per-trade risk even when the margin sizing itself looks
normal (observed live: a 6.4% SL distance at 7x leverage risked ~24% of
equity on a single stop-out).
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


def _raw_cfg(leverage, atr_sl_multiplier=1.5, max_position_risk_pct=8.0,
             position_size_pct_of_equity=50.0):
    return {
        "exchange": {"category": "linear", "symbols": ["XUSDT"], "universe": {"enabled": False}},
        "risk": {
            "position_size_pct_of_equity": position_size_pct_of_equity, "margin_buffer_pct": 0.0,
            "override_entry": {"enabled": False}, "max_leverage": 25,
            "leverage_by_symbol": {"XUSDT": {"min": leverage, "max": leverage}},
            "default_leverage_range": {"min": 5, "max": 8},
            "max_daily_loss_pct": 8.0, "max_concurrent_positions": 4,
            "min_confidence_to_enter": 0.55, "min_order_notional_usdt": 5.0,
            "max_position_risk_pct": max_position_risk_pct,
        },
        "signals": {
            "weights": {"technical": 0.4, "volume": 0.1, "news": 0.15, "polymarket": 0.2, "funding": 0.15},
            "technical": {"timeframes": ["15", "60", "240"], "atr_period": 14, "kline_limit": 200},
            "news": {"feeds": []}, "polymarket": {"keywords": []}, "funding": {"normalize_pct": 0.05},
        },
        "trade_management": {
            "atr_sl_multiplier": atr_sl_multiplier, "atr_tp_multiplier": 2.5,
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


def _make_strategy(tmp_path, **cfg_kwargs):
    secrets = Secrets(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                       newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                       dashboard_token="t")
    cfg = Config(secrets=secrets, raw=_raw_cfg(**cfg_kwargs))
    state = StateStore(str(tmp_path / "state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    client.get_equity_usdt.return_value = 100.0
    client.get_available_balance_usdt.return_value = 1_000_000.0
    client.get_instrument_info.return_value = FakeInst()
    client.round_price.side_effect = lambda symbol, price: round(price, 2)
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 2)
    client.get_last_price.return_value = 100.0
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(confidence=0.9, atr=1.0):
    return {"score": confidence, "confidence": confidence, "direction": "long",
            "atr": atr, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_entry_skipped_when_sl_hit_risk_exceeds_cap(tmp_path):
    # margin=50% of equity, leverage=15x, sl_distance=2.0% -> risk = 0.5*15*2.0 = 15% > cap(8%)
    strategy, state, client = _make_strategy(tmp_path, leverage=15, atr_sl_multiplier=2.0)
    strategy.get_signal = lambda s, force=False: _signal(atr=1.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_not_called()
    assert state.get_trade("XUSDT") is None


def test_entry_allowed_when_sl_hit_risk_is_under_cap(tmp_path):
    # margin=50%, leverage=5x, sl_distance=0.2% -> risk = 0.5*5*0.2 = 0.5%
    strategy, state, client = _make_strategy(tmp_path, leverage=5, atr_sl_multiplier=1.0)
    strategy.get_signal = lambda s, force=False: _signal(atr=0.2)

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()


def test_cap_disabled_when_max_position_risk_pct_is_zero(tmp_path):
    # same numbers as the "exceeds cap" test above (risk=15%) -- must go through when disabled
    strategy, state, client = _make_strategy(tmp_path, leverage=15, atr_sl_multiplier=2.0,
                                               max_position_risk_pct=0.0)
    strategy.get_signal = lambda s, force=False: _signal(atr=1.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()


def test_risk_right_at_the_cap_boundary_is_allowed(tmp_path):
    # margin=50%, leverage=8x, sl_distance=2.0% -> risk = 0.5*8*2.0 = 8.0% == cap(8.0)
    strategy, state, client = _make_strategy(tmp_path, leverage=8, atr_sl_multiplier=2.0,
                                               max_position_risk_pct=8.0)
    strategy.get_signal = lambda s, force=False: _signal(atr=1.0)

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()
