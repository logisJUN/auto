from bot.risk.position_sizing import compute_qty


def test_basic_risk_sizing():
    result = compute_qty(
        equity=1000, risk_per_trade_pct=1.0, entry_price=100, stop_loss_price=95,
        max_leverage=10, qty_step=0.001, min_qty=0.001, min_notional=5.0,
    )
    assert result.ok
    assert result.qty == 2.0  # risk_amount=10, sl_distance=5 -> qty=2
    assert result.notional == 200.0
    assert result.leverage_needed == 0.2


def test_leverage_cap_reduces_qty():
    # sizing wants far more notional than max_leverage allows -> qty gets capped down.
    result = compute_qty(
        equity=100, risk_per_trade_pct=50.0, entry_price=100, stop_loss_price=99,
        max_leverage=2, qty_step=0.01, min_qty=0.01, min_notional=1.0,
    )
    assert result.ok
    assert result.notional <= 100 * 2 + 1e-6


def test_tiny_account_below_min_qty_is_rejected():
    # mirrors a very small (e.g. $26) account sizing a BTC trade with tight risk.
    result = compute_qty(
        equity=26, risk_per_trade_pct=1.5, entry_price=60000, stop_loss_price=59500,
        max_leverage=5, qty_step=0.001, min_qty=0.001, min_notional=5.0,
    )
    assert not result.ok


def test_invalid_stop_price_rejected():
    result = compute_qty(
        equity=1000, risk_per_trade_pct=1.0, entry_price=100, stop_loss_price=100,
        max_leverage=5, qty_step=0.001, min_qty=0.001, min_notional=5.0,
    )
    assert not result.ok
