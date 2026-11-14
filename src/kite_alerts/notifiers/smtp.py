"""SMTP e-mail notifier (STARTTLS by default)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..core.models import Message
from .base import BaseNotifier, DeliveryError


class EmailNotifier(BaseNotifier):
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        sender: str,
        recipients: str | list[str],
        username: str = "",
        password: str = "",
        use_tls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._host = host
        self._port = port
        self._sender = sender
        self._recipients = (
            [recipients]
            if isinstance(recipients, str) and "," not in recipients
            else [
                r.strip()
                for r in (
                    recipients if isinstance(recipients, str) else ",".join(recipients)
                ).split(",")
                if r.strip()
            ]
        )
        self._username = username
        self._password = password
        self._use_tls = use_tls

    def send(self, message: Message) -> None:
        email = EmailMessage()
        email["Subject"] = message.subject or _subject_from(message)
        email["From"] = self._sender
        email["To"] = ", ".join(self._recipients)
        email.set_content(message.text)

        try:
            with smtplib.SMTP(self._host, self._port, timeout=self.timeout) as client:
                client.ehlo()
                if self._use_tls:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self._username:
                    client.login(self._username, self._password)
                client.send_message(email)
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(self.name, f"smtp delivery failed: {exc}") from exc

    def close(self) -> None:
        return None


def _subject_from(message: Message) -> str:
    first_line = message.text.splitlines()[0] if message.text else "kite-alerts"
    return f"[kite-alerts] {first_line[:120]}"
