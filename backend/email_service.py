from __future__ import annotations

import hashlib
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote

VERIFICATION_TTL_HOURS = int(os.getenv("VERIFICATION_TTL_HOURS", "24"))
PASSWORD_RESET_TTL_HOURS = int(os.getenv("PASSWORD_RESET_TTL_HOURS", "1"))


def create_verification_token() -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=VERIFICATION_TTL_HOURS)
    return token, token_hash, expires_at


def hash_verification_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def send_verification_email(email: str, token: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
    backend_url = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    if not host or not sender:
        raise RuntimeError("SMTP is not configured. Add SMTP_HOST and SMTP_FROM to .env")

    verification_url = f"{backend_url}/auth/verify-email?email={quote(email)}&token={quote(token)}"
    message = EmailMessage()
    message["Subject"] = "Confirm your Autonomous QA account"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "Welcome to Autonomous QA.\n\n"
        "Confirm your email address by opening this link:\n"
        f"{verification_url}\n\n"
        f"This link expires in {VERIFICATION_TTL_HOURS} hours.\n"
        "If you did not create this account, you can ignore this email."
    )

    if use_tls:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)


def send_password_reset_email(email: str, token: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
    if not host or not sender:
        raise RuntimeError("SMTP is not configured. Add SMTP_HOST and SMTP_FROM to .env")

    reset_url = f"{frontend_url}/#reset_token={quote(token)}&reset_email={quote(email)}"
    message = EmailMessage()
    message["Subject"] = "Reset your Autonomous QA password"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "We received a request to reset your Autonomous QA password.\n\n"
        "Set a new password by opening this link:\n"
        f"{reset_url}\n\n"
        f"This link expires in {PASSWORD_RESET_TTL_HOURS} hour(s) and can only be used once.\n"
        "If you did not request this, you can ignore this email."
    )

    if use_tls:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
