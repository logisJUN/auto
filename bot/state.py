"""JSON persistence for open trades and the daily loss-limit counter.

Keeping this on disk (instead of only in memory) means a restart of the bot
process (crash, VPS reboot, systemd restart) doesn't lose track of an open
position or reset the daily loss circuit breaker.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def default_state() -> dict:
    return {
        "trades": {},       # symbol -> trade dict (see risk/stop_manager.new_trade)
        "daily": {"date": _today_utc(), "start_equity": None, "realized_pnl": 0.0},
        "history": [],      # list of closed-trade summaries (kept short)
        "stale_cooldowns": {},  # symbol -> {"side": ..., "until_ts": ...}
    }


class StateStore:
    def __init__(self, path: str | Path = "data/state.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                base = default_state()
                base.update(data)
                return base
            except Exception:
                return default_state()
        return default_state()

    def save(self):
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
            tmp.replace(self.path)

    # -- trades ---------------------------------------------------------
    def get_trade(self, symbol: str) -> dict | None:
        return self._state["trades"].get(symbol)

    def set_trade(self, symbol: str, trade: dict | None):
        if trade is None:
            self._state["trades"].pop(symbol, None)
        else:
            self._state["trades"][symbol] = trade
        self.save()

    def open_trade_count(self) -> int:
        return len(self._state["trades"])

    def record_closed_trade(self, summary: dict):
        self._state["history"].append(summary)
        self._state["history"] = self._state["history"][-200:]
        self.save()

    # -- daily loss tracking ---------------------------------------------------------
    def seed_daily(self, date: str, start_equity: float, realized_pnl: float):
        """Sets today's daily-loss-tracking record. Callers should reconstruct
        realized_pnl from the exchange's own history when the existing local
        record doesn't match today (see Strategy._sync_daily_state), rather
        than assuming 0 -- a local state reset (a real risk on Render's free
        plan) would otherwise silently defeat the daily loss circuit breaker.
        """
        self._state["daily"] = {"date": date, "start_equity": start_equity, "realized_pnl": realized_pnl}
        self.save()

    def add_realized_pnl(self, pnl: float):
        self._state["daily"]["realized_pnl"] = self._state["daily"].get("realized_pnl", 0.0) + pnl
        self.save()

    def daily_loss_pct(self) -> float:
        daily = self._state["daily"]
        start = daily.get("start_equity") or 0.0
        if start <= 0:
            return 0.0
        pnl = daily.get("realized_pnl", 0.0)
        return max(0.0, -pnl / start * 100.0)

    # -- stale-exit re-entry cooldown ---------------------------------------------------------
    def set_stale_cooldown(self, symbol: str, side: str, until_ts: float):
        """Records that `symbol` was just closed for going nowhere (stale_timeout)
        while positioned `side` -- until_ts blocks a same-direction re-entry until
        then, so the bot doesn't immediately re-open the same losing, going-nowhere
        trade and repeat the round-trip fee (observed live: a symbol stuck in a
        tight range kept getting shorted, stale-timed-out, and re-shorted).
        """
        self._state["stale_cooldowns"][symbol] = {"side": side, "until_ts": until_ts}
        self.save()

    def get_stale_cooldown(self, symbol: str) -> dict | None:
        return self._state.get("stale_cooldowns", {}).get(symbol)

    # -- misc ---------------------------------------------------------
    def get_last_summary_date(self) -> str | None:
        return self._state.get("last_summary_date")

    def set_last_summary_date(self, date: str):
        self._state["last_summary_date"] = date
        self.save()

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._state, default=str))
