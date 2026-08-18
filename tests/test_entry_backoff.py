"""trade_management.{entry_fail_backoff_min,sl_tp_fail_backoff_min,
permanent_error_backoff_min}: after a failed entry attempt, block retrying the
same symbol for a while instead of hammering it every tick. Observed live:
a symbol retried 12x on insufficient margin before finally filling, another
kept failing SL/TP attach and eating a round-trip fee on every attempt, and a
third was permanently blocked by a regional restriction (ErrCode 110132) and
kept getting retried every rescan regardless.
"""
import time
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitAPIError, BybitClient
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
    "entry_fail_backoff_min": 5, "sl_tp_fail_backoff_min": 60, "permanent_error_backoff_min": 1440,
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


def _signal(confidence=0.9, direction="long"):
    return {"score": confidence, "confidence": confidence, "direction": direction,
            "atr": 1.0, "close": 100.0, "range_high": 0.0, "range_low": 0.0, "components": {}}


# -- try_enter respects an active backoff ---------------------------------------------------------

def test_entry_blocked_while_backoff_active(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    state.set_entry_backoff("XUSDT", until_ts=time.time() + 300, reason="insufficient margin")
    strategy.get_signal = lambda s, force=False: _signal()

    strategy.try_enter("XUSDT")

    client.get_equity_usdt.assert_not_called()  # bailed out before even fetching equity
    client.open_position.assert_not_called()


def test_entry_allowed_once_backoff_expires(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    state.set_entry_backoff("XUSDT", until_ts=time.time() - 1, reason="insufficient margin")
    strategy.get_signal = lambda s, force=False: _signal()
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }

    strategy.try_enter("XUSDT")

    client.open_position.assert_called_once()


# -- _open() records a backoff on failure ---------------------------------------------------------

def test_order_placement_failure_records_short_backoff(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.open_position.side_effect = BybitAPIError(
        "place_order rejected by Bybit: ab not enough for new order (ErrCode: 110007)")

    strategy.try_enter("XUSDT")

    backoff = state.get_entry_backoff("XUSDT")
    assert backoff is not None
    remaining_min = (backoff["until_ts"] - time.time()) / 60
    assert 4.9 < remaining_min <= 5.0


def test_regional_restriction_records_long_backoff(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.open_position.side_effect = BybitAPIError(
        "place_order rejected by Bybit: permission denied (ErrCode: 110132)")

    strategy.try_enter("XUSDT")

    backoff = state.get_entry_backoff("XUSDT")
    remaining_min = (backoff["until_ts"] - time.time()) / 60
    assert 1439 < remaining_min <= 1440


def test_missing_product_agreement_records_long_backoff(tmp_path):
    """e.g. CLUSDT (crude oil) rejecting with ErrCode 110125 -- a retry will
    never succeed without agreeing to a product-specific terms page, so this
    should get the same long backoff as a regional restriction.
    """
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.open_position.side_effect = BybitAPIError(
        "place_order rejected by Bybit: You must agree to the Crude Oil "
        "Trading Terms before trading this contract. (ErrCode: 110125)")

    strategy.try_enter("XUSDT")

    backoff = state.get_entry_backoff("XUSDT")
    remaining_min = (backoff["until_ts"] - time.time()) / 60
    assert 1439 < remaining_min <= 1440


def test_sl_tp_attach_failure_records_backoff(tmp_path):
    from bot.exchange.bybit_client import BybitAPIError as _BAE

    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 0.0, "take_profit": 0.0,  # never attached
    }
    client.update_trading_stop.side_effect = _BAE("repair failed")

    strategy.try_enter("XUSDT")

    client.close_position.assert_called_once()  # force-closed for safety
    backoff = state.get_entry_backoff("XUSDT")
    assert backoff is not None
    remaining_min = (backoff["until_ts"] - time.time()) / 60
    assert 59 < remaining_min <= 60


def test_sl_tp_attach_failure_escalates_to_the_long_tier_on_repeat(tmp_path):
    """A symbol whose SL/TP attach keeps failing the same way (not just bad
    timing once) shouldn't keep retrying and paying a round-trip fee on the
    forced safety-close every hour forever -- observed live: PUMPFUNUSDT
    failed 4 separate times, ~60-90min apart, each one a wasted fee with zero
    chance of success.
    """
    from bot.exchange.bybit_client import BybitAPIError as _BAE

    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 0.0, "take_profit": 0.0,
    }
    client.update_trading_stop.side_effect = _BAE("repair failed")

    strategy.try_enter("XUSDT")  # 1st failure -- still the short 60min tier
    remaining_min_1 = (state.get_entry_backoff("XUSDT")["until_ts"] - time.time()) / 60
    assert 59 < remaining_min_1 <= 60

    state.set_entry_backoff("XUSDT", until_ts=time.time() - 1, reason="expired")  # simulate the wait
    strategy.try_enter("XUSDT")  # 2nd consecutive failure -- escalates

    backoff = state.get_entry_backoff("XUSDT")
    remaining_min_2 = (backoff["until_ts"] - time.time()) / 60
    assert 1439 < remaining_min_2 <= 1440
    assert "x2" in backoff["reason"]


def test_sl_tp_failure_streak_resets_after_a_successful_entry(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    strategy.get_signal = lambda s, force=False: _signal()
    state.record_sl_tp_failure("XUSDT")  # simulate one prior failure

    client.open_position.return_value = {}
    client.get_position.return_value = {
        "symbol": "XUSDT", "side": "Buy", "size": 1.0, "entry_price": 100.0,
        "unrealized_pnl": 0.0, "position_idx": 0, "stop_loss": 90.0, "take_profit": 110.0,
    }

    strategy.try_enter("XUSDT")  # succeeds this time

    assert state.get_sl_tp_failures("XUSDT") == 0
