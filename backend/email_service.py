from __future__ import annotations

import hashlib
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
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


def _smtp_config() -> tuple[str, str, str, str, int, bool]:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
    if not host or not sender:
        raise RuntimeError("SMTP is not configured. Add SMTP_HOST and SMTP_FROM to .env")
    return host, username, password, sender, port, use_tls


def _send_message(message: EmailMessage) -> None:
    host, username, password, _, port, use_tls = _smtp_config()

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


def _build_action_email(
    *,
    to_email: str,
    subject: str,
    eyebrow: str,
    headline: str,
    intro: str,
    button_label: str,
    action_url: str,
    expiry_copy: str,
    security_copy: str,
) -> EmailMessage:
    _, _, _, sender, _, _ = _smtp_config()
    escaped_url = escape(action_url, quote=True)
    escaped_email = escape(to_email)
    preview = f"{intro} {expiry_copy}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message.set_content(
        f"{headline}\n\n"
        f"{intro}\n\n"
        f"{button_label}: {action_url}\n\n"
        f"{expiry_copy}\n"
        f"{security_copy}\n\n"
        "If the button does not work, copy and paste the link into your browser."
    )
    message.add_alternative(
        f"""\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(subject)}</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f6fb;color:#172033;font-family:Arial,Helvetica,sans-serif;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{escape(preview)}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6fb;padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e6eaf2;border-radius:18px;overflow:hidden;box-shadow:0 18px 45px rgba(23,32,51,0.10);">
            <tr>
              <td style="padding:28px 32px;background:#111827;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:0;">Autonomous QA</td>
                    <td align="right" style="color:#9ca3af;font-size:12px;text-transform:uppercase;">Secure account</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:36px 32px 20px;">
                <div style="margin:0 0 14px;color:#2563eb;font-size:12px;font-weight:700;text-transform:uppercase;">{escape(eyebrow)}</div>
                <h1 style="margin:0 0 16px;color:#111827;font-size:28px;line-height:1.25;font-weight:700;">{escape(headline)}</h1>
                <p style="margin:0;color:#4b5563;font-size:16px;line-height:1.65;">{escape(intro)}</p>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:10px 32px 30px;">
                <a href="{escaped_url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;padding:15px 26px;border-radius:8px;">{escape(button_label)}</a>
              </td>
            </tr>
            <tr>
              <td style="padding:0 32px 30px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:12px;">
                  <tr>
                    <td style="padding:18px 20px;color:#4b5563;font-size:14px;line-height:1.6;">
                      <strong style="display:block;margin-bottom:6px;color:#111827;">Link details</strong>
                      {escape(expiry_copy)} This email was sent to <span style="color:#111827;">{escaped_email}</span>.
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:0 32px 34px;color:#6b7280;font-size:13px;line-height:1.65;">
                <p style="margin:0 0 12px;">{escape(security_copy)}</p>
                <p style="margin:0;">Button not working? Copy this link into your browser:<br><a href="{escaped_url}" style="color:#2563eb;word-break:break-all;">{escaped_url}</a></p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""",
        subtype="html",
    )
    return message


def send_verification_email(email: str, token: str) -> None:
    backend_url = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    verification_url = f"{backend_url}/auth/verify-email?email={quote(email)}&token={quote(token)}"
    message = _build_action_email(
        to_email=email,
        subject="Confirm your Autonomous QA account",
        eyebrow="Account activation",
        headline="Activate your account",
        intro="Welcome to Autonomous QA. Confirm your email address to finish setting up your account and start using the dashboard.",
        button_label="Activate account",
        action_url=verification_url,
        expiry_copy=f"This activation link expires in {VERIFICATION_TTL_HOURS} hours.",
        security_copy="If you did not create this account, no action is needed and you can safely ignore this email.",
    )
    _send_message(message)


def send_password_reset_email(email: str, token: str) -> None:
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
    reset_url = f"{frontend_url}/#reset_token={quote(token)}&reset_email={quote(email)}"
    message = _build_action_email(
        to_email=email,
        subject="Reset your Autonomous QA password",
        eyebrow="Password reset",
        headline="Reset your password",
        intro="We received a request to reset the password for your Autonomous QA account. Use the secure button below to choose a new password.",
        button_label="Reset password",
        action_url=reset_url,
        expiry_copy=f"This password reset link expires in {PASSWORD_RESET_TTL_HOURS} hour(s) and can only be used once.",
        security_copy="If you did not request a password reset, you can safely ignore this email and your password will stay unchanged.",
    )
    _send_message(message)
