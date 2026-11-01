"""Administrator operations: user management, platform statistics, audit trail, report queue.

Event moderation lives in event_service.moderate_event. There is intentionally NO function that
changes a user's role: role escalation is impossible through the API (admins are created only
with scripts/create_admin.py).
"""

import uuid

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.utils import escape_like
from app.models.audit import AuditLog
from app.models.enums import EventStatus, RegistrationStatus, ReportStatus, Role, TicketStatus, UserStatus
from app.models.event import Event, EventReport
from app.models.registration import Registration
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.admin import AuditLogOut, PlatformStatsOut
from app.services.audit_service import record_audit
from app.services.auth_service import revoke_all_refresh_tokens


async def list_users(
    db: AsyncSession, *, role: str | None, status: str | None, q: str | None, offset: int, limit: int
) -> tuple[list[User], int]:
    clauses = []
    if role:
        clauses.append(User.role == role)
    if status:
        clauses.append(User.status == status)
    if q:
        pattern = f"%{escape_like(q)}%"
        clauses.append(User.full_name.ilike(pattern, escape="\\") | User.email.ilike(pattern, escape="\\"))
    total = (await db.execute(select(func.count()).select_from(User).where(*clauses))).scalar_one()
    result = await db.execute(
        select(User).where(*clauses).order_by(User.created_at.desc(), User.id).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), total


async def set_user_status(
    db: AsyncSession, admin: User, user_id: uuid.UUID, new_status: str, reason: str | None, request: Request | None
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if user.id == admin.id:
        raise ForbiddenError("You cannot change the status of your own account")
    if user.role == Role.ADMIN.value:
        raise ForbiddenError("Administrator accounts cannot be suspended through the API")
    if user.status == new_status:
        raise ConflictError(f"The account is already {new_status}")

    previous = user.status
    user.status = new_status
    if new_status == UserStatus.SUSPENDED.value:
        await revoke_all_refresh_tokens(db, user.id)  # kill existing sessions immediately
    record_audit(
        db,
        actor=admin,
        action="user.suspend" if new_status == UserStatus.SUSPENDED.value else "user.reactivate",
        target_type="user",
        target_id=user.id,
        details={"from": previous, "to": new_status, "reason": reason},
        request=request,
    )
    await db.commit()
    await db.refresh(user)
    return user


async def platform_stats(db: AsyncSession) -> PlatformStatsOut:
    async def grouped(column) -> dict[str, int]:
        rows = (await db.execute(select(column, func.count()).group_by(column))).all()
        return {key: count for key, count in rows}

    users_by_role = await grouped(User.role)
    users_by_status = await grouped(User.status)
    events_by_status = await grouped(Event.status)
    registrations_by_status = await grouped(Registration.status)
    checked_in = (
        await db.execute(select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.USED.value))
    ).scalar_one()
    open_reports = (
        await db.execute(
            select(func.count()).select_from(EventReport).where(EventReport.status == ReportStatus.OPEN.value)
        )
    ).scalar_one()
    return PlatformStatsOut(
        users_total=sum(users_by_role.values()),
        users_by_role=users_by_role,
        users_suspended=users_by_status.get(UserStatus.SUSPENDED.value, 0),
        events_total=sum(events_by_status.values()),
        events_by_status=events_by_status,
        events_pending_review=events_by_status.get(EventStatus.PENDING.value, 0),
        registrations_active=registrations_by_status.get(RegistrationStatus.CONFIRMED.value, 0),
        registrations_cancelled=registrations_by_status.get(RegistrationStatus.CANCELLED.value, 0),
        tickets_checked_in=checked_in,
        open_reports=open_reports,
    )


async def list_audit_logs(
    db: AsyncSession,
    *,
    action: str | None,
    actor_id: uuid.UUID | None,
    target_type: str | None,
    offset: int,
    limit: int,
) -> tuple[list[AuditLogOut], int]:
    clauses = []
    if action:
        clauses.append(AuditLog.action.ilike(f"{escape_like(action)}%", escape="\\"))  # prefix match, e.g. "event."
    if actor_id:
        clauses.append(AuditLog.actor_id == actor_id)
    if target_type:
        clauses.append(AuditLog.target_type == target_type)
    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*clauses))).scalar_one()
    result = await db.execute(
        select(AuditLog).where(*clauses).order_by(AuditLog.created_at.desc(), AuditLog.id).offset(offset).limit(limit)
    )
    items = [
        AuditLogOut(
            id=row.id,
            actor_id=row.actor_id,
            actor_name=row.actor.full_name if row.actor else None,
            actor_email=row.actor.email if row.actor else None,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            details=row.details,
            ip_address=row.ip_address,
            created_at=row.created_at,
        )
        for row in result.scalars().all()
    ]
    return items, total


async def list_reports(
    db: AsyncSession, *, status: str | None, offset: int, limit: int
) -> tuple[list[EventReport], int]:
    clauses = [EventReport.status == status] if status else []
    total = (await db.execute(select(func.count()).select_from(EventReport).where(*clauses))).scalar_one()
    result = await db.execute(
        select(EventReport)
        .where(*clauses)
        .order_by(EventReport.created_at.desc(), EventReport.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total
