"""
Outbound email abstraction.

There was no email integration anywhere in the platform before this module
(routes_billing.py notes "no email/SMTP integration exists"). Production email
delivery is NOT approved, so only local implementations exist here and no real
provider is wired:

- ``ConsoleEmailSender`` (default, ``EMAIL_BACKEND=console``): one log line per
  message with the recipient's DOMAIN and the subject only. The body is never
  logged, because verification emails carry a single-use token.
- ``InMemoryEmailSender`` (``EMAIL_BACKEND=memory``): keeps messages in a list
  in this process. Used by the tests.
- ``FileEmailSender`` (``EMAIL_BACKEND=file``): writes each message as an .eml
  file under ``EMAIL_FILE_DIR`` so a developer can click a verification link
  locally. Refused outside ENVIRONMENT development/test.

Adding a real provider later means one more class implementing ``send`` and a
branch in ``_build``; callers do not change. Sending is best effort and never
raises into the caller (``send_safely``).
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage as _MimeMessage
from typing import Protocol

from backend.app.core.config import get_settings

logger = logging.getLogger("nextgen.email")


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    # A short machine label ("email_verification", "duplicate_registration"),
    # so tests and logs can tell messages apart without reading the body.
    kind: str = "generic"


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else "?"


class ConsoleEmailSender:
    """Logs that a message would have been sent. Never the body, never the local part."""

    async def send(self, message: EmailMessage) -> None:
        logger.info("EMAIL (console mock, not delivered): kind=%s to=*@%s subject=%r",
                    message.kind, _domain(message.to), message.subject)


class InMemoryEmailSender:
    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)

    def clear(self) -> None:
        self.outbox.clear()


class FileEmailSender:
    def __init__(self, directory: str, from_address: str) -> None:
        if not directory:
            raise RuntimeError("EMAIL_BACKEND=file needs EMAIL_FILE_DIR")
        self.directory = directory
        self.from_address = from_address

    async def send(self, message: EmailMessage) -> None:
        os.makedirs(self.directory, exist_ok=True)
        mime = _MimeMessage()
        mime["From"] = self.from_address
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = os.path.join(self.directory, f"{stamp}-{message.kind}-{uuid.uuid4().hex[:8]}.eml")
        with open(path, "wb") as fh:
            fh.write(bytes(mime))
        logger.info("EMAIL (file mock, not delivered): kind=%s to=*@%s written", message.kind, _domain(message.to))


_sender: EmailSender | None = None


def _build() -> EmailSender:
    settings = get_settings()
    backend = (settings.EMAIL_BACKEND or "console").strip().lower()
    if backend == "memory":
        return InMemoryEmailSender()
    if backend == "file":
        if settings.ENVIRONMENT not in ("development", "test"):
            raise RuntimeError("EMAIL_BACKEND=file is only allowed in development/test")
        return FileEmailSender(settings.EMAIL_FILE_DIR, settings.EMAIL_FROM_ADDRESS)
    if backend != "console":
        # A typo or a provider name that does not exist yet: fall back to the
        # console mock rather than silently pretending to deliver.
        logger.warning("Unknown EMAIL_BACKEND %r; using the console mock (no email is delivered).", backend)
    return ConsoleEmailSender()


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = _build()
    return _sender


def set_email_sender(sender: EmailSender | None) -> None:
    """Replace the process-wide sender (tests). None rebuilds from settings on next use."""
    global _sender
    _sender = sender


async def send_safely(message: EmailMessage) -> bool:
    """Send, never raising. Returns False (and logs without PII) on failure."""
    try:
        await get_email_sender().send(message)
        return True
    except Exception:  # noqa: BLE001 - a mail failure must never break the request
        logger.exception("Email send failed: kind=%s to=*@%s", message.kind, _domain(message.to))
        return False
