"""Optional Telegram push notifications, for keeping an eye on the bot from an
iPad/phone lock screen without opening the dashboard. No-ops silently if
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID aren't set; never raises (a notification
failure must never take down the trading loop).
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger("bot.notify")


class Notifier:
    def __init__(self, bot_token: str | None, chat_id: str | None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

    def send(self, text: str):
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=8,
            )
        except Exception as exc:
            logger.warning("telegram notify failed: %s", exc)
