import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import client_ip
from app.models.audit import AuditLog
from app.models.user import User


def record_audit(
    db: AsyncSession,
    *,
    actor: User | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Stage an audit row in the caller's transaction, so the action and its audit trail commit
    (or roll back) together. Never put secrets/tokens in `details`."""
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details,
        ip_address=client_ip(request),
    )
    db.add(entry)
    return entry
