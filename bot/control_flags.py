"""Tiny Postgres-backed persistence for bot control flags -- currently just
aggressive_btc's pause toggle.

aggressive_btc.pause_flag_path() is a local sentinel file: fast to check on
every try_enter() call, no network round-trip needed. But Render's free
plan has no persistent disk (see render_app.py's module docstring), so a
restart -- a new deploy, a crash, or the free-plan service sleeping and
waking back up on the next request -- wipes it, which silently un-pauses a
user's explicit stop (observed live: paused in the evening, found running
again "다음날"/the next day with no deploy in between -- a free-plan
sleep+wake cycle is the most likely cause). When DATABASE_URL is set (same
requirement as bot/newsfeed.py, for the same reason), this module gives
that local file a durable shadow: the dashboard's toggle route writes
through here as well as the local file, and aggressive_btc.main()'s loop
resyncs the local file from here once per iteration, before any strategy
gets a chance to try_enter() -- so even a freshly restarted process with no
local file recovers the real paused state within one loop iteration
instead of silently defaulting to "not paused".

Every function here is a no-op (returns None/False, never raises) when
DATABASE_URL isn't set -- aggressive_btc's local-file-only behavior is then
exactly what it was before this module existed.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("bot.control_flags")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ENABLED = bool(DATABASE_URL)

_schema_ready = False


def _get_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_control_flags (
                name TEXT PRIMARY KEY,
                value BOOLEAN NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
    conn.commit()
    _schema_ready = True


def set_flag(name: str, value: bool) -> None:
    """Best-effort, never raises. A failure here only means the durable
    copy didn't update -- the local file (which the caller sets regardless,
    see dashboard.app.toggle_aggressive_btc) still governs this process
    until its next restart.
    """
    if not ENABLED:
        return
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("control_flags: failed to connect to DATABASE_URL", exc_info=True)
        return
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_control_flags (name, value, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """, (name, value, time.time()))
        conn.commit()
    except Exception:
        logger.warning("control_flags: failed to set flag %s", name, exc_info=True)
    finally:
        conn.close()


def get_flag(name: str) -> bool | None:
    """None means "unknown" (disabled, connection failed, or never
    explicitly set) -- deliberately distinct from False, so a caller can
    tell "nothing to resync from" apart from "explicitly set to false" and
    leave the local file alone in the former case.
    """
    if not ENABLED:
        return None
    try:
        conn = _get_conn()
    except Exception:
        logger.warning("control_flags: failed to connect to DATABASE_URL", exc_info=True)
        return None
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_control_flags WHERE name = %s", (name,))
            row = cur.fetchone()
        return bool(row[0]) if row else None
    except Exception:
        logger.warning("control_flags: failed to get flag %s", name, exc_info=True)
        return None
    finally:
        conn.close()
