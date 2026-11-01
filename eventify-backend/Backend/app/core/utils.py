import secrets
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def generate_registration_reference() -> str:
    """Human-friendly unique registration identifier, e.g. REG-9F3A1B2C4D."""
    return f"REG-{secrets.token_hex(5).upper()}"
