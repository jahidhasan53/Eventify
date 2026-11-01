"""Cryptographic helpers: Argon2id hashing, JWT access tokens, opaque refresh/reset tokens,
CSRF tokens and HMAC-signed QR ticket tokens."""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.utils import utcnow

# --------------------------------------------------------------------------- passwords
# Argon2id with argon2-cffi's RFC 9106 "low memory" defaults (64 MiB, t=3, p=4).
_hasher = PasswordHasher(type=Type.ID)
# Used to spend the same time when an e-mail address is unknown (reduces user enumeration by timing).
DUMMY_PASSWORD_HASH = _hasher.hash("eventify-timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# --------------------------------------------------------------------------- JWT access tokens
def create_access_token(user_id: uuid.UUID, *, expires_delta: timedelta | None = None) -> tuple[str, int]:
    """Return (token, expires_in_seconds). Only the user id is embedded; role/status are always
    re-read from the database so they cannot be forged or go stale."""
    settings = get_settings()
    now = utcnow()
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + delta,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm)
    return token, int(delta.total_seconds())


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # pin the algorithm: no "alg: none" / key confusion
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Access token has expired") from None
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid authentication credentials") from None
    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid authentication credentials")
    return payload


# --------------------------------------------------------------------------- opaque tokens
def generate_opaque_token() -> str:
    """High-entropy random token for refresh tokens and password resets."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 digest used to store opaque tokens (a DB leak must not reveal usable tokens)."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --------------------------------------------------------------------------- QR ticket tokens
def generate_ticket_nonce() -> str:
    return secrets.token_urlsafe(32)


def _ticket_signature(ticket_id: uuid.UUID, nonce: str) -> str:
    digest = hmac.new(get_settings().ticket_signing_key, f"{ticket_id}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_ticket_token(ticket_id: uuid.UUID, nonce: str) -> str:
    """Opaque token placed in the QR code: '<ticket-id-hex>.<HMAC signature>'.

    It contains no personal data. Forging one requires the server-side signing key, and the
    per-ticket random nonce (stored in the DB) is mixed into the signature.
    """
    return f"{ticket_id.hex}.{_ticket_signature(ticket_id, nonce)}"


def parse_ticket_token(token: str) -> tuple[uuid.UUID, str] | None:
    """Split a token into (ticket_id, signature) or None if malformed. Does NOT verify the signature."""
    try:
        id_part, signature = token.strip().split(".", 1)
        if len(id_part) != 32 or not signature or len(signature) > 64:
            return None
        return uuid.UUID(hex=id_part), signature
    except ValueError:
        return None


def verify_ticket_signature(ticket_id: uuid.UUID, nonce: str, signature: str) -> bool:
    return hmac.compare_digest(_ticket_signature(ticket_id, nonce).encode(), signature.encode())


def seconds_until(moment: datetime) -> int:
    return max(int((moment - utcnow()).total_seconds()), 0)
