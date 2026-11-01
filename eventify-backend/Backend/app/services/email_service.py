"""Configurable e-mail delivery. Backends: console (development only), smtp, disabled.

`send_email` never raises: a mail outage must not break a registration or a password reset
request. SMTP credentials come from environment variables and are never logged.
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from app.core.config import get_settings

logger = logging.getLogger("eventify.email")


async def send_email(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    backend = settings.email_backend

    if backend == "disabled":
        return

    if backend == "console":
        # Development convenience only (blocked in production by config validation). The body is
        # logged so a developer can follow password-reset links without an SMTP server.
        if settings.environment == "development":
            logger.info("EMAIL to=%s subject=%s\n%s", to, subject, body)
        return

    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            start_tls=settings.smtp_use_starttls,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - delivery failure is logged, never propagated
        logger.exception("Failed to send e-mail to %s (subject=%s)", to, subject)


async def send_password_reset_email(to: str, full_name: str, reset_link: str, expires_minutes: int) -> None:
    body = (
        f"Hello {full_name},\n\n"
        "We received a request to reset your Eventify password. Use the link below to choose a new one:\n\n"
        f"{reset_link}\n\n"
        f"This link expires in {expires_minutes} minutes and can be used once. "
        "If you did not request this, you can safely ignore this e-mail.\n"
    )
    await send_email(to, "Reset your Eventify password", body)
