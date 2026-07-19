"""compute_qty_fixed_margin's floor-based math can leave floating-point noise
(e.g. 4.1000000000000005) in the qty it returns. Bybit rejects a qty string
with that kind of noise outright (ErrCode 10001, "Qty invalid") since it
doesn't match the symbol's actual step precision -- observed live on CLUSDT.
_open()/_close_and_settle() must run the qty through client.round_qty() before
it ever reaches open_position()/close_position().
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.state import StateStore
from bot.strategy import Strategy

RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["NOISYUSDT"], "universe": {"enabled": False}},
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
        "flash_move_pct": 1.2, "flash_move_window_sec": 45,
        "reversal_exit_score": 0.4, "reversal_exit_confidence": 0.6,
        "stale_exit_after_min": 60, "stale_exit_max_move_pct": 0.5,
        "range_trade": {"enabled": True, "edge_atr_mult": 0.5,
                        "max_range_width_atr_mult": 4.0,
                        "atr_sl_multiplier": 1.0, "atr_tp_multiplier": 1.0},
    },
    "loop": {"signal_refresh_sec": 120},
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
    # Real round_qty behavior (the thing under test): floor-then-round to the
    # step's decimal precision, same as bot/exchange/bybit_client.py.
    client.round_qty.side_effect = lambda symbol, qty: round(qty, 1)
    client.get_last_price.return_value = 100.0
    client.open_position.return_value = {}
    # Non-zero stop_loss/take_profit -> the new post-entry SL/TP verification
    # in _open() sees them as already attached and doesn't try to repair them.
    client.get_position.return_value = {
        "symbol": "X", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }
    client.close_position.return_value = {}
    client.get_closed_pnl.return_value = {
        "closed_pnl": 1.0, "avg_exit_price": 100.0, "updated_time_ms": 99999999999999,
    }
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


def _signal(confidence, direction="long"):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": 1.0, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


def test_open_position_uses_rounded_qty_not_raw_noisy_float(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(0.6)

    strategy.try_enter("NOISYUSDT")

    assert client.round_qty.called, "round_qty must be called before placing the order"
    sent_qty = client.open_position.call_args.args[2]
    assert sent_qty == round(sent_qty, 1), "qty sent to Bybit must match the exchange's step precision"

    trade = state.get_trade("NOISYUSDT")
    assert trade["qty"] == sent_qty, "stored trade qty should match what was actually sent"


def test_close_position_also_uses_rounded_qty(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal(0.6)
    strategy.try_enter("NOISYUSDT")

    trade = state.get_trade("NOISYUSDT")
    # simulate floating-point noise creeping into stored state (e.g. from a
    # pre-fix trade, or any other arithmetic on qty)
    trade["qty"] = trade["qty"] + 1e-12
    state.set_trade("NOISYUSDT", trade)

    strategy._close_and_settle("NOISYUSDT", trade, "test_reason")

    close_qty = client.close_position.call_args.args[2]
    assert close_qty == round(close_qty, 1)
