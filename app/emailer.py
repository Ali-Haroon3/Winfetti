"""Verification email delivery behind an interface, mirroring fulfillment:
tests inject the stub, dev without a provider logs instead of sending, and
production sends over SMTP (works with the SES/Postmark/Mailgun SMTP
endpoints) once smtp_host and email_from are configured."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings, get_settings

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


class SmtpEmailSender:
    """Sends over SMTP. A raised exception surfaces as a 500 on
    POST /v1/me/email — the token stays valid, so the user just requests
    again (the route sends after commit for exactly this reason)."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def send_verification(self, *, to: str, token: str) -> None:
        s = self._settings
        msg = EmailMessage()
        msg["From"] = s.email_from
        msg["To"] = to
        msg["Subject"] = "Verify your Winfetti email"
        lines = [
            "Confirm this address to enable gift-card redemptions.",
            "",
            f"Your verification code: {token}",
        ]
        if s.email_verify_link_template:
            lines += ["", "Or open: " + s.email_verify_link_template.format(token=token)]
        lines += ["", "If you didn't request this, you can ignore this email."]
        msg.set_content("\n".join(lines))

        if s.smtp_ssl:
            smtp = smtplib.SMTP_SSL(
                s.smtp_host,
                s.smtp_port,
                timeout=30,
                context=ssl.create_default_context(),
            )
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        with smtp:
            if s.smtp_starttls and not s.smtp_ssl:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)


def build_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.smtp_host and settings.email_from:
        if settings.email_verify_link_template:
            # Fail at startup, not on the first user's send.
            try:
                rendered = settings.email_verify_link_template.format(token="PROBE")
            except (KeyError, IndexError) as exc:
                raise ValueError(
                    "EMAIL_VERIFY_LINK_TEMPLATE may only reference {token}"
                ) from exc
            if "PROBE" not in rendered:
                raise ValueError(
                    "EMAIL_VERIFY_LINK_TEMPLATE must contain a {token} placeholder"
                )
        logger.info(
            "email: SMTP sender via %s:%d as %s",
            settings.smtp_host,
            settings.smtp_port,
            settings.email_from,
        )
        return SmtpEmailSender(settings)
    logger.warning("no email provider configured; verification emails are logged only")
    return LoggingEmailSender()
