from bot.state import StateStore


def test_seed_daily_sets_values_directly(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-5.0)
    daily = state.snapshot()["daily"]
    assert daily == {"date": "2026-01-01", "start_equity": 100.0, "realized_pnl": -5.0}


def test_daily_loss_pct_reflects_current_equity_vs_start(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-8.0)
    assert state.daily_loss_pct(current_equity=92.0) == 8.0


def test_daily_loss_pct_includes_unrealized_loss_not_just_realized(tmp_path):
    """The whole point of basing this on live equity instead of realized_pnl:
    a big unrealized loss on a still-open position must count toward the
    daily-loss limit, not just what's already been closed.
    """
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-2.0)
    # realized_pnl alone would say only 2% down, but equity reflects an
    # additional 6% of unrealized loss sitting on an open position
    assert state.daily_loss_pct(current_equity=92.0) == 8.0


def test_daily_loss_pct_zero_when_equity_recovered_above_start(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-8.0)
    assert state.daily_loss_pct(current_equity=101.0) == 0.0


def test_add_realized_pnl_accumulates_on_top_of_seeded_value(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-3.0)
    state.add_realized_pnl(-2.0)
    assert state.snapshot()["daily"]["realized_pnl"] == -5.0


def test_last_summary_date_roundtrip(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    assert state.get_last_summary_date() is None
    state.set_last_summary_date("2026-01-02")
    assert state.get_last_summary_date() == "2026-01-02"

    # persists across a fresh StateStore load from the same file
    reloaded = StateStore(str(tmp_path / "state.json"))
    assert reloaded.get_last_summary_date() == "2026-01-02"


def test_stale_cooldown_roundtrip(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    assert state.get_stale_cooldown("XUSDT") is None

    state.set_stale_cooldown("XUSDT", "short", until_ts=12345.0)
    cooldown = state.get_stale_cooldown("XUSDT")
    assert cooldown == {"side": "short", "until_ts": 12345.0}

    # persists across a fresh StateStore load from the same file
    reloaded = StateStore(str(tmp_path / "state.json"))
    assert reloaded.get_stale_cooldown("XUSDT") == {"side": "short", "until_ts": 12345.0}


def test_stale_cooldown_defaults_to_empty_on_old_state_file(tmp_path):
    # simulates loading a state.json written before stale_cooldowns existed
    path = tmp_path / "state.json"
    path.write_text('{"trades": {}, "daily": {"date": "x", "start_equity": null, "realized_pnl": 0.0}, "history": []}')
    state = StateStore(str(path))
    assert state.get_stale_cooldown("XUSDT") is None


def test_entry_backoff_roundtrip(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    assert state.get_entry_backoff("XUSDT") is None

    state.set_entry_backoff("XUSDT", until_ts=12345.0, reason="insufficient margin")
    backoff = state.get_entry_backoff("XUSDT")
    assert backoff == {"until_ts": 12345.0, "reason": "insufficient margin"}

    reloaded = StateStore(str(tmp_path / "state.json"))
    assert reloaded.get_entry_backoff("XUSDT") == {"until_ts": 12345.0, "reason": "insufficient margin"}


def test_skip_reason_roundtrip_and_overwrite(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    assert state.get_skip_reason("XUSDT") is None

    state.set_skip_reason("XUSDT", "신뢰도 부족")
    skip = state.get_skip_reason("XUSDT")
    assert skip["reason"] == "신뢰도 부족"
    assert "ts" in skip

    # overwritten, not appended, on the next tick's skip
    state.set_skip_reason("XUSDT", "변동성 부족")
    assert state.get_skip_reason("XUSDT")["reason"] == "변동성 부족"

    reloaded = StateStore(str(tmp_path / "state.json"))
    assert reloaded.get_skip_reason("XUSDT")["reason"] == "변동성 부족"


def test_clear_skip_reason(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.set_skip_reason("XUSDT", "신뢰도 부족")
    state.clear_skip_reason("XUSDT")
    assert state.get_skip_reason("XUSDT") is None

    # clearing something never set is a harmless no-op
    state.clear_skip_reason("YUSDT")
    assert state.get_skip_reason("YUSDT") is None


def test_daily_reset_request_roundtrip(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    assert state.consume_daily_reset_request() is False  # nothing requested yet

    state.request_daily_reset()
    assert state.consume_daily_reset_request() is True
    # consuming clears it -- a second check right after finds nothing
    assert state.consume_daily_reset_request() is False


def test_daily_reset_request_visible_from_a_separate_statestore_instance(tmp_path):
    """The whole point: the dashboard process writes the request through its
    own StateStore instance, and the bot's separately-running process (its
    own StateStore instance, same underlying file) must see it.
    """
    dashboard_side = StateStore(str(tmp_path / "state.json"))
    bot_side = StateStore(str(tmp_path / "state.json"))

    dashboard_side.request_daily_reset()

    assert bot_side.consume_daily_reset_request() is True
