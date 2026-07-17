"""Turns (equity, risk %, entry, stop-loss) into an order quantity.

The core idea: qty is sized so that if price hits the stop-loss, the realized
loss equals `risk_per_trade_pct` of equity -- independent of leverage. Leverage
only determines how much margin that position consumes; if the notional needed
to hit the target risk would require more leverage than `max_leverage` allows,
qty is capped down (accepting a smaller position / smaller max loss) rather than
ever exceeding the configured leverage ceiling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizingResult:
    ok: bool
    qty: float = 0.0
    notional: float = 0.0
    leverage_needed: float = 0.0
    reason: str = ""


def compute_qty(
    equity: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_loss_price: float,
    max_leverage: float,
    qty_step: float,
    min_qty: float,
    min_notional: float,
) -> SizingResult:
    sl_distance = abs(entry_price - stop_loss_price)
    if sl_distance <= 0 or entry_price <= 0:
        return SizingResult(ok=False, reason="invalid entry/stop price")

    risk_amount = equity * (risk_per_trade_pct / 100.0)
    if risk_amount <= 0:
        return SizingResult(ok=False, reason="no risk budget (equity or risk_pct is zero)")

    raw_qty = risk_amount / sl_distance
    notional = raw_qty * entry_price

    max_notional = equity * max_leverage
    if notional > max_notional:
        notional = max_notional
        raw_qty = notional / entry_price

    qty = math.floor(raw_qty / qty_step) * qty_step if qty_step > 0 else raw_qty
    notional = qty * entry_price
    leverage_needed = notional / equity if equity > 0 else 0.0

    if qty < min_qty:
        return SizingResult(ok=False, qty=qty, notional=notional, leverage_needed=leverage_needed,
                             reason=f"qty {qty} below exchange minimum {min_qty}")
    if notional < min_notional:
        return SizingResult(ok=False, qty=qty, notional=notional, leverage_needed=leverage_needed,
                             reason=f"notional {notional:.2f} USDT below configured minimum {min_notional}")

    return SizingResult(ok=True, qty=qty, notional=notional, leverage_needed=leverage_needed)


def compute_qty_fixed_margin(
    equity: float,
    position_pct_of_equity: float,
    leverage: float,
    entry_price: float,
    qty_step: float,
    min_qty: float,
    min_notional: float,
) -> SizingResult:
    """Sizes a position by margin allocation instead of stop-loss risk: this trade
    always uses `position_pct_of_equity`% of equity as margin, at `leverage`x. Unlike
    compute_qty, the loss if the stop-loss is hit is NOT held to a fixed % of equity --
    it depends on leverage and how far away the stop-loss is.
    """
    if entry_price <= 0 or equity <= 0:
        return SizingResult(ok=False, reason="invalid equity/entry price")

    margin = equity * (position_pct_of_equity / 100.0)
    notional = margin * leverage

    qty = math.floor(notional / entry_price / qty_step) * qty_step if qty_step > 0 else notional / entry_price
    notional = qty * entry_price
    leverage_needed = notional / equity if equity > 0 else 0.0

    if qty < min_qty:
        return SizingResult(ok=False, qty=qty, notional=notional, leverage_needed=leverage_needed,
                             reason=f"qty {qty} below exchange minimum {min_qty}")
    if notional < min_notional:
        return SizingResult(ok=False, qty=qty, notional=notional, leverage_needed=leverage_needed,
                             reason=f"notional {notional:.2f} USDT below configured minimum {min_notional}")

    return SizingResult(ok=True, qty=qty, notional=notional, leverage_needed=leverage_needed)
