from __future__ import annotations

import base64
import crypt
import hashlib
import hmac
import json
import ipaddress
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .orm_models import User

JWT_ALGORITHM = "HS256"
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-this-secret")
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "1440"))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_jwt(payload: dict[str, object]) -> str:
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    encoded_header = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    encoded_payload = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    message = f"{encoded_header}.{encoded_payload}".encode()
    signature = hmac.new(JWT_SECRET.encode(), message, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64encode(signature)}"


def _decode_jwt(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed token")
    message = f"{parts[0]}.{parts[1]}".encode()
    expected = hmac.new(JWT_SECRET.encode(), message, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64decode(parts[2])):
        raise ValueError("Invalid signature")
    payload = json.loads(_b64decode(parts[1]))
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise ValueError("Expired token")
    return payload


def hash_password(password: str) -> str:
    return crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        candidate = crypt.crypt(password, password_hash)
        return bool(candidate) and hmac.compare_digest(candidate, password_hash)
    except (TypeError, ValueError):
        return False


def create_access_token(user: User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    return _encode_jwt({"sub": str(user.id), "email": user.email, "exp": int(expires.timestamp())})


def get_current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = _decode_jwt(token)
        user_id = int(payload.get("sub", ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is unavailable")
    return user


def validate_public_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Only public http/https URLs are allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    blocked_names = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
    if hostname in blocked_names or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise HTTPException(status_code=400, detail="Local and internal hostnames are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="The target hostname could not be resolved")
    if not addresses:
        raise HTTPException(status_code=400, detail="The target hostname has no address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise HTTPException(status_code=400, detail="Private and internal network targets are not allowed")
    return value
