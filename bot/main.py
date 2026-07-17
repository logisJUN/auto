"""Entry point. Run with: python -m bot.main

Loops forever: polls fast while any position is open (for flash-move emergency
exits), polls slower while flat (waiting for an entry signal). Intended to run
as a systemd service on a small always-on VPS -- see deploy/ and README.md.
"""
from __future__ import annotations

import logging
import time

from bot.config import load_config
from bot.exchange.bybit_client import BybitClient
from bot.logger import setup_logging
from bot.notify import Notifier
from bot.state import StateStore
from bot.strategy import Strategy


def main():
    cfg = load_config()
    log_dir = setup_logging()
    logger = logging.getLogger("bot.main")

    client = BybitClient(
        api_key=cfg.secrets.bybit_api_key,
        api_secret=cfg.secrets.bybit_api_secret,
        testnet=cfg.secrets.bybit_testnet,
        category=cfg.get("exchange", "category", default="linear"),
    )
    state = StateStore("data/state.json")
    notifier = Notifier(cfg.secrets.telegram_bot_token, cfg.secrets.telegram_chat_id)
    strategy = Strategy(client, cfg, state, notifier, str(log_dir))

    mode = "TESTNET (모의투자)" if cfg.secrets.bybit_testnet else "LIVE (실거래, 실제 자금 사용)"
    logger.info("bot starting | mode=%s | symbols=%s", mode, strategy.symbols)
    notifier.send(f"봇 시작됨 - {mode} - 종목: {', '.join(strategy.symbols)}")

    fast_poll = cfg.get("loop", "fast_poll_sec", default=8)
    idle_poll = cfg.get("loop", "idle_poll_sec", default=20)

    while True:
        loop_start = time.time()
        try:
            strategy.tick()
        except Exception:
            logger.exception("unhandled error in main loop tick")

        has_open_position = state.open_trade_count() > 0
        interval = fast_poll if has_open_position else idle_poll
        elapsed = time.time() - loop_start
        time.sleep(max(1.0, interval - elapsed))


if __name__ == "__main__":
    main()
