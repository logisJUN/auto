"""Combined entrypoint for deploying this project as a single Render Web Service.

Render exposes exactly one HTTP port per service and does not share disks
between services, so the trading bot and the read-only dashboard -- which are
independent systemd services in deploy/ for a VPS -- run in the same process
here instead: the bot loop runs in a background thread, and the Flask
dashboard is served (via gunicorn) on the port Render assigns.

Start command (see render.yaml):
    gunicorn render_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

Must use exactly one gunicorn worker: the bot thread only starts once, in
this module's process, on import.
"""
from __future__ import annotations

import logging
import threading

from bot.main import main as run_bot_forever
from dashboard.app import app  # noqa: F401 (gunicorn imports this as the WSGI app)

logger = logging.getLogger("bot.render")

_start_lock = threading.Lock()
_started = False


def _start_bot_thread():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(target=run_bot_forever, name="bot-loop", daemon=True)
        thread.start()
        logger.info("bot loop started in background thread")


_start_bot_thread()


if __name__ == "__main__":
    # Local/manual run without gunicorn: python render_app.py
    import os

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
