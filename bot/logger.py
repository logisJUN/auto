"""Logging setup: readable console + rotating file for operational logs, plus a
separate append-only decisions.jsonl audit trail (one JSON line per entry/exit/
adjustment decision) so you can review *why* the bot did something, from the
dashboard or by hand.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path


def setup_logging(log_dir: str | Path = "logs", level=logging.INFO) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("bot")
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    return log_dir


def log_decision(log_dir: str | Path, record: dict):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), **record}
    with open(log_dir / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_recent_decisions(log_dir: str | Path, limit: int = 50) -> list[dict]:
    path = Path(log_dir) / "decisions.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out


_CONFIDENCE_BUCKETS = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]


def _bucket_label(conf: float) -> str:
    for lo, hi in _CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return "?"


def _trade_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"count": 0, "win_rate": None, "total_pnl": 0.0, "avg_pnl": 0.0}
    wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
    total = sum(t["pnl"] or 0 for t in trades)
    return {"count": n, "win_rate": wins / n, "total_pnl": total, "avg_pnl": total / n}


def compute_performance_summary(log_dir: str | Path, limit_lines: int = 5000) -> dict:
    """Pairs each 'entry' decision with the next 'exit' for the same symbol to
    reconstruct closed trades with their entry-time confidence, then reports win
    rate / pnl overall, by confidence bucket (trend trades only -- range trades
    don't have a directional confidence), trend vs. range, and by symbol (which
    alt keeps losing, as opposed to the aggregate hiding it in a wash).

    This is the "did our confidence actually predict wins" check: if forecast
    accuracy were tracked at all, this is that check, applied after the fact
    instead of never.
    """
    path = Path(log_dir) / "decisions.jsonl"
    if not path.exists():
        return {
            "trades": [], "overall": _trade_stats([]), "by_confidence": {}, "by_symbol": {},
            "by_type": {"trend": _trade_stats([]), "range": _trade_stats([])},
        }

    lines = path.read_text(encoding="utf-8").splitlines()[-limit_lines:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            continue

    by_symbol: dict[str, list[dict]] = {}
    for e in events:
        symbol = e.get("symbol")
        if symbol:
            by_symbol.setdefault(symbol, []).append(e)

    trades = []
    for symbol, evs in by_symbol.items():
        pending_entry = None
        for e in evs:
            event = e.get("event")
            # position_adopted (an orphaned exchange position the bot found
            # and started tracking, e.g. after a state reset) counts as an
            # entry too -- otherwise its eventual exit has no matching entry
            # and gets silently dropped from every stat here, undercounting
            # win rate for exactly the trades most likely to happen after a
            # restart, which is common on this setup.
            if event in ("entry", "position_adopted"):
                pending_entry = e
            elif event == "exit" and pending_entry is not None:
                signal = pending_entry.get("signal") or {}
                trades.append({
                    "symbol": symbol,
                    "is_range_trade": "range_high" in pending_entry,
                    "confidence": signal.get("confidence"),
                    "side": pending_entry.get("side"),
                    "pnl": e.get("pnl"),
                    "fees_paid": e.get("fees_paid"),
                    "reason": e.get("reason"),
                    "entry_ts": pending_entry.get("ts"),
                    "exit_ts": e.get("ts"),
                })
                pending_entry = None

    trend_trades = [t for t in trades if not t["is_range_trade"]]
    range_trades = [t for t in trades if t["is_range_trade"]]

    by_confidence: dict[str, dict] = {}
    for t in trend_trades:
        if t["confidence"] is None:
            continue
        label = _bucket_label(t["confidence"])
        by_confidence.setdefault(label, []).append(t)
    by_confidence = {label: _trade_stats(ts) for label, ts in by_confidence.items()}

    by_symbol_trades: dict[str, list[dict]] = {}
    for t in trades:
        by_symbol_trades.setdefault(t["symbol"], []).append(t)
    by_symbol = {symbol: _trade_stats(ts) for symbol, ts in by_symbol_trades.items()}
    # worst total_pnl first -- that's the one worth asking "is this alt just bad"
    by_symbol = dict(sorted(by_symbol.items(), key=lambda kv: kv[1]["total_pnl"]))

    return {
        "trades": trades,
        "overall": _trade_stats(trades),
        "by_confidence": by_confidence,
        "by_symbol": by_symbol,
        "by_type": {
            "trend": _trade_stats(trend_trades),
            "range": _trade_stats(range_trades),
        },
    }
