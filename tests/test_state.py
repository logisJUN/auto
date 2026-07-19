from bot.state import StateStore


def test_seed_daily_sets_values_directly(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-5.0)
    daily = state.snapshot()["daily"]
    assert daily == {"date": "2026-01-01", "start_equity": 100.0, "realized_pnl": -5.0}


def test_daily_loss_pct_reflects_seeded_values(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.seed_daily("2026-01-01", start_equity=100.0, realized_pnl=-8.0)
    assert state.daily_loss_pct() == 8.0


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
