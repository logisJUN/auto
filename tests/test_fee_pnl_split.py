"""Strategy._close_and_settle splits a trade's net pnl into gross price
movement vs. implied fees, so a loss can be told apart as "the market moved
against us" vs. "we were basically flat and just paid the round-trip fee" --
previously only the combined net figure was recorded, so this always had to
be guessed at from context.
"""
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.risk import stop_manager
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


def _open_trade(side="short", entry=100.0, qty=1.0):
    return stop_manager.new_trade("XUSDT", side, entry_price=entry, qty=qty, atr=1.0,
                                   cfg=RAW_CFG["trade_management"])


def test_fees_computed_as_gap_between_gross_and_net_when_real_pnl_available(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(side="long", entry=100.0, qty=1.0)
    # price actually moved +2.0 in our favor (gross = 2.0), but the exchange's
    # own net closedPnl is only 1.89 -- the 0.11 gap is the round-trip fee.
    client.get_closed_pnl.return_value = {
        "closed_pnl": 1.89, "avg_exit_price": 102.0, "updated_time_ms": 99999999999999,
    }

    strategy._close_and_settle("XUSDT", trade, "sl_tp_hit", already_closed=True)

    entry = state.snapshot()["history"][-1]
    assert entry["gross_price_pnl"] == 2.0
    assert round(entry["fees_paid"], 10) == 0.11


def test_fees_unknown_when_falling_back_to_price_estimate(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(side="long", entry=100.0, qty=1.0)
    client.get_closed_pnl.return_value = None  # no exchange record yet
    client.get_last_price.return_value = 101.0

    strategy._close_and_settle("XUSDT", trade, "sl_tp_hit", already_closed=True)

    entry = state.snapshot()["history"][-1]
    assert entry["pnl_is_estimate"] is True
    assert entry["fees_paid"] is None
    assert entry["gross_price_pnl"] == 1.0  # the estimate itself IS the gross figure


def test_fee_split_accounts_for_short_side_direction(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    trade = _open_trade(side="short", entry=100.0, qty=1.0)
    # price dropped 3.0 in our favor for a short (gross = 3.0), net after fees 2.85
    client.get_closed_pnl.return_value = {
        "closed_pnl": 2.85, "avg_exit_price": 97.0, "updated_time_ms": 99999999999999,
    }

    strategy._close_and_settle("XUSDT", trade, "sl_tp_hit", already_closed=True)

    entry = state.snapshot()["history"][-1]
    assert entry["gross_price_pnl"] == 3.0
    assert round(entry["fees_paid"], 10) == 0.15
