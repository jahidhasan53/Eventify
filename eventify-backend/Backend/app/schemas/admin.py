import uuid
from datetime import datetime
from typing import Any

from app.schemas.common import ORMModel, StrictModel


class AuditLogOut(ORMModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_name: str | None = None
    actor_email: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    details: dict[str, Any] | None = None
    ip_address: str | None = None
    created_at: datetime


class PlatformStatsOut(StrictModel):
    users_total: int
    users_by_role: dict[str, int]
    users_suspended: int
    events_total: int
    events_by_status: dict[str, int]
    events_pending_review: int
    registrations_active: int
    registrations_cancelled: int
    tickets_checked_in: int
    open_reports: int
