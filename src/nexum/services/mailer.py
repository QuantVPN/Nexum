"""Outbound e-mail through SMTP (settings live in company settings).

The transport is pluggable so tests can capture messages instead of connecting anywhere.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from nexum.models.company import CompanySettings

log = logging.getLogger("nexum.mail")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str


Transport = Callable[[CompanySettings, Message], None]
_transport: Transport | None = None


def set_transport(transport: Transport | None) -> None:
    global _transport
    _transport = transport


def smtp_transport(settings: CompanySettings, message: Message) -> None:
    email = EmailMessage()
    email["From"] = settings.smtp_from or ""
    email["To"] = message.to
    email["Subject"] = message.subject
    email.set_content(message.body)
    with smtplib.SMTP(settings.smtp_host or "", settings.smtp_port, timeout=15) as client:
        if settings.smtp_use_tls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        client.send_message(email)


def send(settings: CompanySettings, to: str, subject: str, body: str) -> bool:
    """Send one e-mail; returns False when e-mail is not configured. Raises on transport errors."""
    if not settings.email_configured:
        return False
    transport = _transport or smtp_transport
    transport(settings, Message(to=to, subject=subject, body=body))
    log.info("sent mail to %s: %s", to, subject)
    return True


def try_send(settings: CompanySettings, to: str, subject: str, body: str) -> bool:
    """Best-effort variant used by notifications: never raises."""
    try:
        return send(settings, to, subject, body)
    except Exception:  # pragma: no cover - depends on the SMTP server
        log.exception("could not send mail to %s", to)
        return False
