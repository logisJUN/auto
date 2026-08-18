"""Optional email notifications (stdlib smtplib only, no extra dependency).

Unlike a local log file, an email lands in the user's inbox regardless of what
happens to the server afterward -- used for periodic summaries that should
survive Render's free-plan disk resets. No-ops silently if SMTP settings
aren't all set; never raises (a notification failure must never take down the
trading loop).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("bot.email_notify")


class EmailNotifier:
    def __init__(self, smtp_host: str | None, smtp_port: int, smtp_user: str | None,
                 smtp_password: str | None, email_from: str | None, email_to: str | None):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.email_from = email_from or smtp_user
        self.email_to = email_to
        self.enabled = bool(smtp_host and smtp_user and smtp_password and email_to)

    def send(self, subject: str, body: str):
        if not self.enabled:
            return
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.email_from
        msg["To"] = self.email_to
        msg.set_content(body)
        try:
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15) as server:
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
        except Exception:
            logger.exception("failed to send email notification")
