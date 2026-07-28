"""The bot's exit brain: initial SL/TP, breakeven + trailing stop, TP extension
on continued momentum, flash-move emergency exit, and signal-reversal exit.

An open trade is represented as a plain dict so it round-trips through
state.py's JSON persistence with no extra work (survives bot restarts). A
short in-memory price history (not persisted -- fine to lose on restart) is
used only to detect flash moves within the last `flash_move_window_sec`.
"""
from __future__ import annotations

import time
from collections import deque


def new_trade(symbol: str, side: str, entry_price: float, qty: float, atr: float, cfg: dict) -> dict:
    sl_mult = cfg.get("atr_sl_multiplier", 1.5)
    tp_mult = cfg.get("atr_tp_multiplier", 2.5)
    if side == "long":
        sl = entry_price - atr * sl_mult
        tp = entry_price + atr * tp_mult
    else:
        sl = entry_price + atr * sl_mult
        tp = entry_price - atr * tp_mult

    risk_distance = abs(entry_price - sl)
    return {
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "qty": qty,
        "initial_sl": sl,
        "initial_tp": tp,
        "current_sl": sl,
        "current_tp": tp,
        "risk_distance": risk_distance,
        "entry_atr": atr,
        "breakeven_moved": False,
        "trailing_active": False,
        "tp_extensions_used": 0,
        "partial_tp_taken": False,
        "partial_realized_pnl": 0.0,
        "opened_at": time.time(),
    }


class FlashMoveTracker:
    """Keeps a short rolling window of (ts, price) per symbol, in memory only."""

    def __init__(self):
        self._history: dict[str, deque] = {}

    def record(self, symbol: str, price: float, window_sec: int):
        dq = self._history.setdefault(symbol, deque())
        now = time.time()
        dq.append((now, price))
        cutoff = now - window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def pct_move(self, symbol: str) -> float:
        dq = self._history.get(symbol)
        if not dq or len(dq) < 2:
            return 0.0
        oldest_price = dq[0][1]
        latest_price = dq[-1][1]
        if oldest_price == 0:
            return 0.0
        return (latest_price - oldest_price) / oldest_price * 100.0


def check_flash_move(tracker: FlashMoveTracker, symbol: str, side: str, cfg: dict, entry_atr_pct: float) -> bool:
    """True if price has moved against the position by more than a threshold
    within the window. The threshold scales with the position's own entry-time
    ATR (as a % of entry price) instead of being one fixed % for every symbol --
    a fixed % is either too loose for a volatile symbol (it can drift a long way
    before a fixed threshold ever fires) or too twitchy for a calm one.
    `entry_atr_pct` is entry_atr / entry_price * 100, computed by the caller.
    """
    mult = cfg.get("flash_move_atr_mult", 1.0)
    min_pct = cfg.get("flash_move_min_pct", 0.8)
    max_pct = cfg.get("flash_move_max_pct", 3.0)
    threshold = max(min_pct, min(max_pct, entry_atr_pct * mult))

    move = tracker.pct_move(symbol)
    if side == "long" and move <= -threshold:
        return True
    if side == "short" and move >= threshold:
        return True
    return False


def check_signal_reversal(trade: dict, agg_signal: dict, cfg: dict) -> bool:
    side = trade["side"]
    score_threshold = cfg.get("reversal_exit_score", 0.4)
    conf_threshold = cfg.get("reversal_exit_confidence", 0.6)
    if agg_signal["confidence"] < conf_threshold:
        return False
    if side == "long" and agg_signal["direction"] == "short" and abs(agg_signal["score"]) >= score_threshold:
        return True
    if side == "short" and agg_signal["direction"] == "long" and abs(agg_signal["score"]) >= score_threshold:
        return True
    return False


def check_stale_position(trade: dict, current_price: float, cfg: dict, capital_pressure: bool = True) -> bool:
    """True if the trade has been open long enough and price has barely moved
    from entry since (stale_exit_max_move_pct) -- i.e. it's going nowhere.
    Applies regardless of whether the trade is currently up or down, so a
    stuck position gives up its slot for a fresh signal elsewhere instead of
    sitting there indefinitely.

    Closing a truly flat position is a guaranteed small loss from round-trip
    fees alone, worth paying only if that capital is actually needed. When
    `capital_pressure` is False (there's free margin/slots -- nothing is
    waiting on this one), the timeout is relaxed to stale_exit_after_min_no_pressure
    instead of the normal stale_exit_after_min, so a flat position gets more
    patience when there's no rush to recycle it.
    """
    timeout_min = cfg.get("stale_exit_after_min", 0)
    if timeout_min <= 0:
        return False
    if not capital_pressure:
        timeout_min = cfg.get("stale_exit_after_min_no_pressure", timeout_min * 4)
    if time.time() - trade["opened_at"] < timeout_min * 60:
        return False

    entry = trade["entry_price"]
    if entry <= 0:
        return False
    max_move_pct = cfg.get("stale_exit_max_move_pct", 0.5)
    moved_pct = abs(current_price - entry) / entry * 100.0
    return moved_pct <= max_move_pct


def profit_r(trade: dict, current_price: float) -> float:
    r = trade["risk_distance"]
    if r <= 0:
        return 0.0
    if trade["side"] == "long":
        return (current_price - trade["entry_price"]) / r
    return (trade["entry_price"] - current_price) / r


def update_trailing_and_tp(trade: dict, current_price: float, atr: float, agg_signal: dict, cfg: dict) -> dict:
    """Mutates trade's current_sl/current_tp in place (favorable direction only) and
    returns {"sl_changed": bool, "tp_changed": bool, "reason": str} for logging/API calls.
    """
    side = trade["side"]
    result = {"sl_changed": False, "tp_changed": False, "reason": ""}
    cur_profit_r = profit_r(trade, current_price)
    atr = max(atr, 1e-9)

    breakeven_rr = cfg.get("breakeven_after_rr", 0.5)
    if not trade["breakeven_moved"] and cur_profit_r >= breakeven_rr:
        entry = trade["entry_price"]
        improves = (side == "long" and entry > trade["current_sl"]) or (side == "short" and entry < trade["current_sl"])
        if improves:
            trade["current_sl"] = entry
            trade["breakeven_moved"] = True
            result["sl_changed"] = True
            result["reason"] += "breakeven;"

    trail_rr = cfg.get("trail_activation_rr", 1.0)
    if cur_profit_r >= trail_rr:
        trade["trailing_active"] = True
        trail_mult = cfg.get("trail_atr_multiplier", 1.2)
        if side == "long":
            candidate_sl = current_price - atr * trail_mult
            if candidate_sl > trade["current_sl"]:
                trade["current_sl"] = candidate_sl
                result["sl_changed"] = True
                result["reason"] += "trail;"
        else:
            candidate_sl = current_price + atr * trail_mult
            if candidate_sl < trade["current_sl"]:
                trade["current_sl"] = candidate_sl
                result["sl_changed"] = True
                result["reason"] += "trail;"

    # extend TP if price has approached it but signals still strongly favor continuation
    remaining = abs(trade["current_tp"] - current_price)
    near_tp = remaining <= atr * 0.5
    same_direction = (side == "long" and agg_signal["direction"] == "long") or \
                      (side == "short" and agg_signal["direction"] == "short")
    strong_enough = agg_signal["confidence"] >= cfg.get("min_confidence_to_enter", 0.55) and abs(agg_signal["score"]) >= 0.3
    max_ext = cfg.get("max_tp_extensions", 3)

    if near_tp and same_direction and strong_enough and trade["tp_extensions_used"] < max_ext:
        step = atr * cfg.get("tp_extend_atr_step", 0.75)
        if side == "long":
            trade["current_tp"] += step
        else:
            trade["current_tp"] -= step
        trade["tp_extensions_used"] += 1
        result["tp_changed"] = True
        result["reason"] += f"tp_extend#{trade['tp_extensions_used']};"

    return result
