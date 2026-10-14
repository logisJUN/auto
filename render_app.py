"""Combined entrypoint for deploying this project as a single Render Web Service.

Render exposes exactly one HTTP port per service and does not share disks
between services, so the trading bot and the read-only dashboard -- which are
independent systemd services in deploy/ for a VPS -- run in the same process
here instead: the bot loop (bot/main.py, multi-symbol) runs in a background
thread, and the Flask dashboard is served (via gunicorn) on the port Render
assigns.

The main bot loop only starts when MAIN_BOT_ENABLED=true -- it's off by
default. Running both it and bot/aggressive_btc.py at once had both polling
Bybit's kline endpoint for BTCUSDT independently and occasionally tripping
Bybit's rate limit (ErrCode 10006); turning the main bot off avoids that and
keeps just the one aggressive_btc strategy running.

bot/aggressive_btc.py (a separate, optional single-symbol strategy --
currently SOXLUSDT, see its own module docstring) can run as a second
background thread in this same process, gated behind
AGGRESSIVE_BTC_ENABLED=true so that deploying updated code alone can never
start it by surprise -- it has to be deliberately turned on via an env var,
same as any other opt-in here.

Start command (see render.yaml):
    gunicorn render_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

Must use exactly one gunicorn worker: the bot thread only starts once, in
this module's process, on import.
"""
from __future__ import annotations

import logging
import os
import threading

from bot.main import main as run_bot_forever
from dashboard.app import app  # noqa: F401 (gunicorn imports this as the WSGI app)

logger = logging.getLogger("bot.render")

_start_lock = threading.Lock()
_started = False


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _start_bot_thread():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

        if _env_bool("MAIN_BOT_ENABLED"):
            thread = threading.Thread(target=run_bot_forever, name="bot-loop", daemon=True)
            thread.start()
            logger.warning("main bot loop started in background thread (MAIN_BOT_ENABLED=true)")
        else:
            logger.warning("main bot loop disabled (MAIN_BOT_ENABLED not set) -- not starting it")

        if _env_bool("AGGRESSIVE_BTC_ENABLED"):
            from bot.aggressive_btc import main as run_aggressive_btc_forever
            agg_thread = threading.Thread(target=run_aggressive_btc_forever,
                                           name="aggressive-btc-loop", daemon=True)
            agg_thread.start()
            logger.warning("aggressive_btc loop started in background thread (AGGRESSIVE_BTC_ENABLED=true)")


_start_bot_thread()


if __name__ == "__main__":
    # Local/manual run without gunicorn: python render_app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

