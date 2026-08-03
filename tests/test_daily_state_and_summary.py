"""Strategy._sync_daily_state(): reconstructs today's realized PnL from
Bybit's own closed-pnl history whenever the local daily record doesn't match
today, instead of the old behavior of just resetting to 0 -- correct for a
normal UTC day rollover (recovers 0) and, more importantly, for a mid-day
state reset (recovers what was actually realized, so the daily loss circuit
breaker isn't silently defeated).

Strategy._maybe_send_daily_summary(): sends a once-per-day email summary
(performance data that would otherwise be lost to a Render free-plan disk
reset), tracked via state.last_summary_date so it doesn't resend the same day.
"""
import time
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import Config, Secrets
from bot.exchange.bybit_client import BybitClient
from bot.notify import Notifier
from bot.state import StateStore
from bot.strategy import Strategy

RAW_CFG = {
    "exchange": {"category": "linear", "symbols": ["XUSDT"], "universe": {"enabled": False}},
    "risk": {
        "position_size_pct_of_equity": 50.0, "margin_buffer_pct": 15.0,
        "override_entry": {"enabled": False}, "max_leverage": 5,
        "leverage_by_symbol": {}, "default_leverage_range": {"min": 5, "max": 8},
        "max_daily_loss_pct": 8.0, "max_concurrent_positions": 4,
        "min_confidence_to_enter": 0.55, "min_order_notional_usdt": 5.0,
    },
    "signals": {
        "weights": {"technical": 0.25, "volume": 0.15, "news": 0.2, "polymarket": 0.4},
        "technical": {"timeframes": ["15", "60", "240"], "atr_period": 14, "kline_limit": 200},
        "news": {"feeds": []}, "polymarket": {"keywords": []},
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
    "loop": {"signal_refresh_sec": 120},
}


def _make_strategy(tmp_path, email_cfg=None):
    secrets_kwargs = dict(bybit_api_key="x", bybit_api_secret="y", bybit_testnet=True,
                           newsapi_key=None, telegram_bot_token=None, telegram_chat_id=None,
                           dashboard_token="t")
    if email_cfg:
        secrets_kwargs.update(email_cfg)
    secrets = Secrets(**secrets_kwargs)
    cfg = Config(secrets=secrets, raw=RAW_CFG)
    state = StateStore(str(tmp_path / "state.json"))
    notifier = Notifier(None, None)
    client = MagicMock(spec=BybitClient)
    return Strategy(client, cfg, state, notifier, str(tmp_path / "logs")), state, client


# -- _sync_daily_state ---------------------------------------------------------

def test_sync_daily_state_noop_when_already_accurate_for_today(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    from bot.strategy import _today_utc
    state.seed_daily(_today_utc(), start_equity=100.0, realized_pnl=-3.0)

    strategy._sync_daily_state(equity=97.0)

    client.get_closed_pnl_since.assert_not_called()
    assert state.snapshot()["daily"]["realized_pnl"] == -3.0


def test_sync_daily_state_reconstructs_from_exchange_after_reset(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    # fresh state (start_equity is None) -- simulates a post-reset StateStore
    client.get_closed_pnl_since.return_value = [
        {"closedPnl": "-2.5"}, {"closedPnl": "1.0"},
    ]

    strategy._sync_daily_state(equity=98.5)

    daily = state.snapshot()["daily"]
    assert daily["realized_pnl"] == -1.5  # -2.5 + 1.0
    assert daily["start_equity"] == 100.0  # 98.5 - (-1.5)
    client.get_closed_pnl_since.assert_called_once()


def test_sync_daily_state_defaults_to_zero_on_exchange_failure(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    client.get_closed_pnl_since.side_effect = RuntimeError("network down")

    strategy._sync_daily_state(equity=100.0)

    daily = state.snapshot()["daily"]
    assert daily["realized_pnl"] == 0.0
    assert daily["start_equity"] == 100.0


# -- _daily_loss_breached ---------------------------------------------------------

def test_try_enter_blocked_by_unrealized_loss_even_when_realized_pnl_is_small(tmp_path):
    """The daily-loss breach check must be based on live equity vs the day's
    start_equity (realized + unrealized combined), not realized_pnl alone --
    otherwise a big unrealized loss sitting on a still-open position would
    never trip the circuit breaker just because nothing's been closed yet.
    """
    strategy, state, client = _make_strategy(tmp_path)
    from bot.strategy import _today_utc
    state.seed_daily(_today_utc(), start_equity=100.0, realized_pnl=-2.0)
    # realized_pnl alone says only -2% -- but equity is actually down 8%
    # (an extra -6% sitting unrealized on an open position elsewhere)
    client.get_equity_usdt.return_value = 92.0

    strategy.try_enter("XUSDT")

    client.open_position.assert_not_called()


# -- _maybe_send_daily_summary ---------------------------------------------------------

def test_daily_summary_noop_when_email_not_configured(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)  # no email config
    strategy._maybe_send_daily_summary()
    assert state.get_last_summary_date() is None


def test_daily_summary_sent_once_and_marks_date(tmp_path):
    email_cfg = dict(email_smtp_host="smtp.example.com", email_smtp_port=465,
                      email_smtp_user="u@example.com", email_smtp_password="pw",
                      email_from=None, email_to="to@example.com")
    strategy, state, client = _make_strategy(tmp_path, email_cfg)
    strategy.email.send = MagicMock()

    strategy._maybe_send_daily_summary()
    assert strategy.email.send.call_count == 1
    from bot.strategy import _today_utc
    assert state.get_last_summary_date() == _today_utc()

    # calling again the same day must not resend
    strategy._maybe_send_daily_summary()
    assert strategy.email.send.call_count == 1


def test_daily_summary_includes_per_trade_list_and_by_symbol_breakdown(tmp_path):
    """The email is the only durable record once state.json/decisions.jsonl
    get wiped by a Render free-plan disk reset -- it must carry enough detail
    (individual trades, by-symbol breakdown) to rebuild a multi-day sample
    from the inbox alone.
    """
    import json
    from bot.strategy import _today_utc

    email_cfg = dict(email_smtp_host="smtp.example.com", email_smtp_port=465,
                      email_smtp_user="u@example.com", email_smtp_password="pw",
                      email_from=None, email_to="to@example.com")
    strategy, state, client = _make_strategy(tmp_path, email_cfg)
    strategy.email.send = MagicMock()

    now = time.time()
    events = [
        {"event": "entry", "symbol": "AAAUSDT", "side": "long", "signal": {"confidence": 0.72}, "ts": now - 100},
        {"event": "exit", "symbol": "AAAUSDT", "reason": "sl_tp_hit", "pnl": -0.21, "fees_paid": 0.11, "ts": now},
    ]
    log_path = Path(strategy.log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    with open(log_path / "decisions.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    strategy._maybe_send_daily_summary()

    body = strategy.email.send.call_args.args[1]
    assert "AAAUSDT" in body
    assert "수수료=0.1100" in body
    assert "종목별 성과" in body


def test_daily_summary_resends_on_a_new_day(tmp_path):
    email_cfg = dict(email_smtp_host="smtp.example.com", email_smtp_port=465,
                      email_smtp_user="u@example.com", email_smtp_password="pw",
                      email_from=None, email_to="to@example.com")
    strategy, state, client = _make_strategy(tmp_path, email_cfg)
    strategy.email.send = MagicMock()
    state.set_last_summary_date("2000-01-01")  # clearly not today

    strategy._maybe_send_daily_summary()

    assert strategy.email.send.call_count == 1


# -- tick() must sync daily state even when no symbol is flat ---------------------------------------------------------

def test_tick_syncs_daily_state_even_when_every_symbol_already_has_a_position(tmp_path):
    """Regression test: _sync_daily_state used to be called only from
    try_enter(), which is never reached for a symbol that already has an open
    trade. If every watched symbol is already positioned (e.g. right after a
    restart re-adopts orphaned positions), the daily-loss reconstruction must
    still run from tick() itself, not be silently skipped for the whole tick.
    """
    strategy, state, client = _make_strategy(tmp_path)
    state.set_trade("XUSDT", {
        "symbol": "XUSDT", "side": "long", "entry_price": 100.0, "qty": 1.0,
        "initial_sl": 95.0, "initial_tp": 110.0, "current_sl": 95.0, "current_tp": 110.0,
        "risk_distance": 5.0, "entry_atr": 5.0, "breakeven_moved": False,
        "trailing_active": False, "tp_extensions_used": 0, "partial_tp_taken": False,
        "partial_realized_pnl": 0.0, "opened_at": 0.0,
    })
    client.get_equity_usdt.return_value = 98.5
    client.get_all_open_positions.return_value = []
    client.get_closed_pnl_since.return_value = [{"closedPnl": "-1.5"}]
    strategy.manage_open_position = MagicMock()  # isolate: not under test here

    strategy.tick()

    client.get_closed_pnl_since.assert_called_once()
    daily = state.snapshot()["daily"]
    assert daily["realized_pnl"] == -1.5
    strategy.manage_open_position.assert_called_once_with("XUSDT")


# -- tick() force-flattens open positions once the daily loss limit trips ---------------------------------------------------------

def _open_trade(symbol="XUSDT"):
    return {
        "symbol": symbol, "side": "long", "entry_price": 100.0, "qty": 1.0,
        "initial_sl": 95.0, "initial_tp": 110.0, "current_sl": 95.0, "current_tp": 110.0,
        "risk_distance": 5.0, "entry_atr": 5.0, "breakeven_moved": False,
        "trailing_active": False, "tp_extensions_used": 0, "partial_tp_taken": False,
        "partial_realized_pnl": 0.0, "opened_at": 0.0,
    }


def test_tick_force_closes_open_positions_once_daily_loss_limit_is_breached(tmp_path):
    """Regression: _daily_loss_breached() only ever blocked try_enter() from
    opening NEW positions -- a position already open and losing when the cap
    tripped kept running its normal manage_open_position() checks and could
    keep bleeding well past the configured limit (observed live: an 8% cap
    closed the day at -9%). Once breached, tick() must force-close every
    still-open position instead of managing it normally.
    """
    strategy, state, client = _make_strategy(tmp_path)
    from bot.strategy import _today_utc
    state.seed_daily(_today_utc(), start_equity=100.0, realized_pnl=0.0)
    state.set_trade("XUSDT", _open_trade())
    client.get_equity_usdt.return_value = 91.0  # -9%, past the 8% cap
    client.get_all_open_positions.return_value = []
    client.close_position.return_value = {}
    client.get_closed_pnl.return_value = None
    client.get_last_price.return_value = 91.0
    strategy.manage_open_position = MagicMock()  # must NOT be reached for XUSDT

    strategy.tick()

    client.close_position.assert_called_once()
    strategy.manage_open_position.assert_not_called()
    assert state.get_trade("XUSDT") is None


def test_tick_manages_normally_when_daily_loss_within_limit(tmp_path):
    strategy, state, client = _make_strategy(tmp_path)
    from bot.strategy import _today_utc
    state.seed_daily(_today_utc(), start_equity=100.0, realized_pnl=0.0)
    state.set_trade("XUSDT", _open_trade())
    client.get_equity_usdt.return_value = 97.0  # -3%, under the 8% cap
    client.get_all_open_positions.return_value = []
    strategy.manage_open_position = MagicMock()

    strategy.tick()

    strategy.manage_open_position.assert_called_once_with("XUSDT")
    client.close_position.assert_not_called()
