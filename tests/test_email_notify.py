from unittest.mock import MagicMock, patch

from bot.email_notify import EmailNotifier


def test_disabled_when_config_incomplete():
    notifier = EmailNotifier(None, 465, None, None, None, None)
    assert not notifier.enabled
    with patch("smtplib.SMTP_SSL") as mock_ssl:
        notifier.send("subject", "body")
        mock_ssl.assert_not_called()


def test_enabled_uses_ssl_for_port_465():
    notifier = EmailNotifier("smtp.example.com", 465, "user@example.com", "pw", None, "to@example.com")
    assert notifier.enabled
    assert notifier.email_from == "user@example.com"  # falls back to smtp_user

    mock_server = MagicMock()
    with patch("smtplib.SMTP_SSL") as mock_ssl:
        mock_ssl.return_value.__enter__.return_value = mock_server
        notifier.send("제목", "내용")
    mock_ssl.assert_called_once_with("smtp.example.com", 465, timeout=15)
    mock_server.login.assert_called_once_with("user@example.com", "pw")
    mock_server.send_message.assert_called_once()


def test_enabled_uses_starttls_for_other_ports():
    notifier = EmailNotifier("smtp.example.com", 587, "user@example.com", "pw", "from@example.com", "to@example.com")

    mock_server = MagicMock()
    with patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        notifier.send("제목", "내용")
    mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@example.com", "pw")


def test_send_failure_does_not_raise():
    notifier = EmailNotifier("smtp.example.com", 465, "user@example.com", "pw", None, "to@example.com")
    with patch("smtplib.SMTP_SSL", side_effect=OSError("network down")):
        notifier.send("subject", "body")  # must not raise
