import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request

from app.api.v1.endpoints.events import Filters
from app.database.connection import DbSession
from app.dependencies.authorization import AdminUser
from app.dependencies.pagination import PageParams
from app.models.enums import Role
from app.models.event import Event
from app.schemas.admin import AuditLogOut, PlatformStatsOut
from app.schemas.common import Envelope, Page, ok, paginated
from app.schemas.event import (
    EventFeatureUpdate,
    EventModerationNote,
    EventModerationReject,
    EventOut,
    EventReportOut,
    EventReportResolve,
    to_event_out,
)
from app.schemas.user import AdminUserStatusUpdate, UserOut
from app.services import admin_service, event_service

# Every route below depends on AdminUser (role == admin, re-checked from the database on each call).
router = APIRouter(prefix="/admin", tags=["Admin"])

EventStatusFilter = Literal["draft", "pending", "approved", "rejected", "published", "cancelled", "completed"]


@router.get("/dashboard", response_model=Envelope[PlatformStatsOut], summary="Platform statistics")
async def dashboard(_: AdminUser, db: DbSession):
    return ok(await admin_service.platform_stats(db))


@router.get("/users", response_model=Envelope[Page[UserOut]], summary="List users")
async def list_users(
    _: AdminUser,
    db: DbSession,
    page: PageParams,
    role: Annotated[Literal["attendee", "organizer", "admin"] | None, Query()] = None,
    status: Annotated[Literal["active", "suspended"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100, description="Search name or e-mail")] = None,
):
    users, total = await admin_service.list_users(
        db, role=role, status=status, q=q.strip() if q else None, offset=page.offset, limit=page.page_size
    )
    return ok(paginated([UserOut.model_validate(u) for u in users], total, page.page, page.page_size))


@router.get("/organizers", response_model=Envelope[Page[UserOut]], summary="List organizers")
async def list_organizers(
    _: AdminUser,
    db: DbSession,
    page: PageParams,
    status: Annotated[Literal["active", "suspended"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
):
    users, total = await admin_service.list_users(
        db, role=Role.ORGANIZER.value, status=status, q=q.strip() if q else None,
        offset=page.offset, limit=page.page_size,
    )
    return ok(paginated([UserOut.model_validate(u) for u in users], total, page.page, page.page_size))


@router.patch("/users/{user_id}/status", response_model=Envelope[UserOut], summary="Suspend or reactivate an account")
async def update_user_status(user_id: uuid.UUID, body: AdminUserStatusUpdate, admin: AdminUser, db: DbSession, request: Request):
    user = await admin_service.set_user_status(db, admin, user_id, body.status, body.reason, request)
    return ok(UserOut.model_validate(user), f"User account is now {user.status}")


# ----------------------------------------------------------------------------- events
@router.get("/events", response_model=Envelope[Page[EventOut]], summary="All events in every status")
async def list_events(
    _: AdminUser,
    db: DbSession,
    page: PageParams,
    filters: Filters,
    status: Annotated[EventStatusFilter | None, Query()] = None,
    organizer_id: Annotated[uuid.UUID | None, Query()] = None,
):
    clauses = event_service.common_clauses(filters)
    if status:
        clauses.append(Event.status == status)
    if organizer_id:
        clauses.append(Event.organizer_id == organizer_id)
    events, total = await event_service.list_events(
        db, clauses, sort_by=filters.sort_by, order=filters.order, offset=page.offset, limit=page.page_size
    )
    items = [to_event_out(e, include_private=True) for e in events]
    return ok(paginated(items, total, page.page, page.page_size))


async def _moderate(action: str, event_id: uuid.UUID, admin, db, request, background, reason: str | None):
    event = await event_service.get_event(db, event_id)
    moderated = await event_service.moderate_event(
        db, event, action, admin, reason=reason, request=request, background=background
    )
    return to_event_out(moderated, include_private=True)


@router.patch("/events/{event_id}/approve", response_model=Envelope[EventOut], summary="Approve a pending event")
async def approve_event(
    event_id: uuid.UUID, admin: AdminUser, db: DbSession, request: Request, background: BackgroundTasks,
    body: EventModerationNote | None = None,
):
    return ok(await _moderate("approve", event_id, admin, db, request, background, body.reason if body else None), "Event approved")


@router.patch("/events/{event_id}/reject", response_model=Envelope[EventOut], summary="Reject a pending event (reason required)")
async def reject_event(
    event_id: uuid.UUID, body: EventModerationReject, admin: AdminUser, db: DbSession, request: Request,
    background: BackgroundTasks,
):
    return ok(await _moderate("reject", event_id, admin, db, request, background, body.reason), "Event rejected")


@router.patch("/events/{event_id}/publish", response_model=Envelope[EventOut], summary="Publish an approved event")
async def publish_event(
    event_id: uuid.UUID, admin: AdminUser, db: DbSession, request: Request, background: BackgroundTasks,
    body: EventModerationNote | None = None,
):
    return ok(await _moderate("publish", event_id, admin, db, request, background, body.reason if body else None), "Event published")


@router.patch("/events/{event_id}/unpublish", response_model=Envelope[EventOut], summary="Take a published event offline")
async def unpublish_event(
    event_id: uuid.UUID, admin: AdminUser, db: DbSession, request: Request, background: BackgroundTasks,
    body: EventModerationNote | None = None,
):
    return ok(await _moderate("unpublish", event_id, admin, db, request, background, body.reason if body else None), "Event unpublished")


@router.patch("/events/{event_id}/feature", response_model=Envelope[EventOut], summary="Feature / unfeature a published event")
async def feature_event(event_id: uuid.UUID, body: EventFeatureUpdate, admin: AdminUser, db: DbSession, request: Request):
    event = await event_service.get_event(db, event_id)
    updated = await event_service.set_featured(db, event, admin, body.featured, request)
    return ok(to_event_out(updated, include_private=True), "Event updated")


# ----------------------------------------------------------------------------- reported events
@router.get("/reports", response_model=Envelope[Page[EventReportOut]], summary="Reported events queue")
async def list_reports(
    _: AdminUser,
    db: DbSession,
    page: PageParams,
    status: Annotated[Literal["open", "resolved", "dismissed"] | None, Query()] = "open",
):
    reports, total = await admin_service.list_reports(db, status=status, offset=page.offset, limit=page.page_size)
    return ok(paginated([EventReportOut.model_validate(r) for r in reports], total, page.page, page.page_size))


@router.patch("/reports/{report_id}", response_model=Envelope[EventReportOut], summary="Resolve or dismiss a report")
async def resolve_report(report_id: uuid.UUID, body: EventReportResolve, admin: AdminUser, db: DbSession, request: Request):
    report = await event_service.get_report(db, report_id)
    resolved = await event_service.resolve_report(db, report, admin, body.status, body.note, request)
    return ok(EventReportOut.model_validate(resolved), "Report updated")


# ----------------------------------------------------------------------------- audit trail
@router.get("/audit-logs", response_model=Envelope[Page[AuditLogOut]], summary="Audit trail of sensitive actions")
async def audit_logs(
    _: AdminUser,
    db: DbSession,
    page: PageParams,
    action: Annotated[str | None, Query(max_length=80, description="Action prefix, e.g. 'event.' or 'user.suspend'")] = None,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    target_type: Annotated[str | None, Query(max_length=40)] = None,
):
    items, total = await admin_service.list_audit_logs(
        db, action=action, actor_id=actor_id, target_type=target_type, offset=page.offset, limit=page.page_size
    )
    return ok(paginated(items, total, page.page, page.page_size))
