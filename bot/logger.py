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
