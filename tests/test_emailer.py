"""SMTP sender selection and delivery (SMTP itself is faked)."""

import smtplib

import pytest

from app.emailer import LoggingEmailSender, SmtpEmailSender, build_email_sender


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL; records everything."""

    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.context = context
        self.starttls_called = False
        self.login_args = None
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg):
        self.messages.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _configure(settings, **overrides):
    settings.smtp_host = "smtp.example.com"
    settings.email_from = "rewards@winfetti.example"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def test_defaults_to_logging_sender(settings):
    assert isinstance(build_email_sender(), LoggingEmailSender)


def test_smtp_sender_selected_when_configured(settings):
    _configure(settings)
    assert isinstance(build_email_sender(), SmtpEmailSender)


def test_send_uses_starttls_and_login_and_contains_token(settings, fake_smtp):
    _configure(settings, smtp_username="user", smtp_password="pass")
    build_email_sender().send_verification(to="p@example.com", token="tok-123")

    (conn,) = fake_smtp.instances
    assert (conn.host, conn.port) == ("smtp.example.com", 587)
    assert conn.starttls_called
    assert conn.login_args == ("user", "pass")
    (msg,) = conn.messages
    assert msg["From"] == "rewards@winfetti.example"
    assert msg["To"] == "p@example.com"
    assert "tok-123" in msg.get_content()


def test_link_template_is_rendered_into_the_body(settings, fake_smtp):
    _configure(
        settings,
        email_verify_link_template="https://winfetti.example/verify?token={token}",
    )
    build_email_sender().send_verification(to="p@example.com", token="tok-9")
    (msg,) = fake_smtp.instances[0].messages
    assert "https://winfetti.example/verify?token=tok-9" in msg.get_content()


def test_implicit_tls_skips_starttls(settings, fake_smtp):
    _configure(settings, smtp_ssl=True, smtp_port=465)
    build_email_sender().send_verification(to="p@example.com", token="t")
    (conn,) = fake_smtp.instances
    assert conn.port == 465
    assert conn.context is not None  # SMTP_SSL got a TLS context
    assert not conn.starttls_called


def test_anonymous_smtp_skips_login(settings, fake_smtp):
    _configure(settings)  # no smtp_username
    build_email_sender().send_verification(to="p@example.com", token="t")
    assert fake_smtp.instances[0].login_args is None


@pytest.mark.parametrize(
    "template",
    [
        "https://winfetti.example/verify",  # no {token} placeholder
        "https://winfetti.example/verify?t={typo}",  # unknown placeholder
    ],
)
def test_bad_link_template_fails_at_startup(settings, template):
    _configure(settings, email_verify_link_template=template)
    with pytest.raises(ValueError):
        build_email_sender()
