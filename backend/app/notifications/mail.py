"""Email providers behind one small interface.

* ``outbox`` (development default): each message is written as a JSON file to
  ``<data dir>/mail-outbox`` (owner-only permissions). Nothing leaves the machine; open the file
  to follow an activation or reset link locally.
* ``resend``: HTTPS API (``RESEND_API_KEY``, ``MAIL_FROM``).
* ``smtp``: any SMTP relay (``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USERNAME``, ``SMTP_PASSWORD``,
  ``SMTP_STARTTLS``, ``MAIL_FROM``).
* ``disabled``: messages are dropped; the event is logged without the message body.

Providers raise ``DeliveryError`` on failure; callers decide whether to retry. Message bodies may
contain single-use links, so they are never logged.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import uuid
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage as MimeMessage
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.jsvalues import now_iso

log = logging.getLogger("governance.mail")


@dataclass
class EmailMessage:
    to: str
    subject: str
    text: str
    template: str = ""
    # Provider idempotency key: the same key is never delivered twice by providers that support it.
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)


class DeliveryError(Exception):
    """The provider did not accept the message. ``permanent``: retrying cannot help."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class MailProvider(Protocol):
    name: str

    def send(self, message: EmailMessage) -> str:
        """Delivers the message and returns the provider's message id."""


class OutboxProvider:
    name = "outbox"

    def __init__(self, directory: Path):
        self.directory = directory

    def send(self, message: EmailMessage) -> str:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        message_id = f"outbox-{uuid.uuid4().hex}"
        path = self.directory / f"{now_iso().replace(':', '-')}-{message_id}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"id": message_id, "at": now_iso(), **asdict(message)}, handle, ensure_ascii=False, indent=2)
        return message_id


class DisabledProvider:
    name = "disabled"

    def send(self, message: EmailMessage) -> str:
        log.warning("Email delivery is disabled; message dropped", extra={"event": "mail_dropped", "template": message.template})
        return "disabled"


class ResendProvider:
    name = "resend"

    def __init__(self, api_key: str, sender: str):
        self.api_key = api_key
        self.sender = sender

    def send(self, message: EmailMessage) -> str:
        try:
            response = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": "Bearer " + self.api_key, "Idempotency-Key": message.idempotency_key},
                json={"from": self.sender, "to": [message.to], "subject": message.subject, "text": message.text},
                timeout=15,
            )
        except httpx.HTTPError as error:
            raise DeliveryError(f"provider unreachable ({type(error).__name__})") from None
        if response.status_code in (400, 401, 403, 404, 422):
            raise DeliveryError(f"provider rejected the message (HTTP {response.status_code})", permanent=True)
        if not response.is_success:
            raise DeliveryError(f"provider error (HTTP {response.status_code})")
        return str(response.json().get("id") or "")


class SmtpProvider:
    name = "smtp"

    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, message: EmailMessage) -> str:
        s = self.settings
        mime = MimeMessage()
        mime["From"], mime["To"], mime["Subject"] = s.mail_from, message.to, message.subject
        mime["Message-ID"] = f"<{message.idempotency_key}@governance-desk>"
        mime.set_content(message.text)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_username:
                    smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(mime)
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryError("recipient refused", permanent=True) from None
        except (smtplib.SMTPException, OSError) as error:
            raise DeliveryError(f"SMTP delivery failed ({type(error).__name__})") from None
        return mime["Message-ID"]


def build_mailer(settings: Settings) -> MailProvider:
    provider = settings.governance_mail_provider
    if provider == "resend":
        return ResendProvider(settings.resend_api_key.get_secret_value(), settings.mail_from)
    if provider == "smtp":
        return SmtpProvider(settings)
    if provider == "disabled":
        return DisabledProvider()
    return OutboxProvider(settings.outbox_dir)
