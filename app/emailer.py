"""Verification email delivery behind an interface, mirroring fulfillment:
tests inject the stub, and dev without a provider logs instead of sending.
Wire a real provider (Postmark/SES/etc.) by implementing EmailSender and
returning it from build_email_sender."""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send_verification(self, *, to: str, token: str) -> None: ...


class LoggingEmailSender:
    """Dev fallback: logs instead of sending. Tokens in logs are acceptable
    only because this sender is never selected once a real provider is
    wired in build_email_sender."""

    def send_verification(self, *, to: str, token: str) -> None:
        logger.info("verification email for %s: token=%s", to, token)


class StubEmailSender:
    """Test double: records what would have been sent."""

    def __init__(self):
        self.sent: list[dict] = []

    def send_verification(self, *, to: str, token: str) -> None:
        self.sent.append({"to": to, "token": token})


def build_email_sender() -> EmailSender:
    logger.warning("no email provider configured; verification emails are logged only")
    return LoggingEmailSender()
