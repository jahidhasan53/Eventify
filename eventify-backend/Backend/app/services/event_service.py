"""Event business logic: querying, ownership, validation, lifecycle (draft -> pending -> approved ->
published -> completed/cancelled) and admin moderation."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import BackgroundTasks, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.utils import escape_like, utcnow
from app.models.enums import (
    PUBLIC_EVENT_STATUSES,
    EventStatus,
    NotificationType,
    RegistrationStatus,
    ReportStatus,
    Role,
    TicketStatus,
)
from app.models.event import Event, EventReport
from app.models.registration import Registration
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.event import EventCreate, EventReportCreate, EventUpdate, find_event_rule_violation
from app.services.audit_service import record_audit
from app.services.notification_service import notify, notify_many

SORT_COLUMNS = {
    "start_datetime": Event.start_datetime,
    "created_at": Event.created_at,
    "title": Event.title,
    "ticket_price": Event.ticket_price,
    "registered_count": Event.registered_count,
}

_REQUIRED_UPDATE_FIELDS = {
    "title", "description", "category", "start_datetime", "end_datetime", "registration_deadline",
    "event_type", "capacity", "ticket_type", "ticket_price",
}
_SIGNIFICANT_FIELDS = {
    "start_datetime", "end_datetime", "registration_deadline", "venue", "location", "event_type",
}


@dataclass
class EventFilters:
    q: str | None = None
    category: str | None = None
    location: str | None = None
    event_type: str | None = None
    ticket_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_past: bool = False
    status: str | None = None  # used by organizer/admin listings only
    sort_by: str = "start_datetime"
    order: str = "asc"


# --------------------------------------------------------------------------- querying
def common_clauses(f: EventFilters) -> list[Any]:
    clauses: list[Any] = []
    if f.q:
        clauses.append(Event.title.ilike(f"%{escape_like(f.q)}%", escape="\\"))
    if f.category:
        clauses.append(func.lower(Event.category) == f.category.lower())
    if f.location:
        pattern = f"%{escape_like(f.location)}%"
        clauses.append(Event.location.ilike(pattern, escape="\\") | Event.venue.ilike(pattern, escape="\\"))
    if f.event_type:
        clauses.append(Event.event_type == f.event_type)
    if f.ticket_type:
        clauses.append(Event.ticket_type == f.ticket_type)
    if f.date_from:
        clauses.append(Event.start_datetime >= datetime.combine(f.date_from, time.min, tzinfo=timezone.utc))
    if f.date_to:
        clauses.append(
            Event.start_datetime < datetime.combine(f.date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )
    return clauses


def public_clauses(f: EventFilters) -> list[Any]:
    """Only published events (plus completed ones when include_past) are ever public."""
    clauses = common_clauses(f)
    if f.include_past:
        clauses.append(Event.status.in_(PUBLIC_EVENT_STATUSES))
    else:
        clauses.append(Event.status == EventStatus.PUBLISHED.value)
        clauses.append(Event.end_datetime > utcnow())
    return clauses


async def list_events(
    db: AsyncSession, clauses: list[Any], *, sort_by: str, order: str, offset: int, limit: int
) -> tuple[list[Event], int]:
    total = (await db.execute(select(func.count()).select_from(Event).where(*clauses))).scalar_one()
    column = SORT_COLUMNS.get(sort_by, Event.start_datetime)
    ordering = column.desc() if order == "desc" else column.asc()
    result = await db.execute(
        select(Event).where(*clauses).order_by(ordering, Event.id).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), total


async def list_categories(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Event.category).where(Event.status.in_(PUBLIC_EVENT_STATUSES)).distinct().order_by(Event.category)
    )
    return [row for row in result.scalars().all()]


async def get_event(db: AsyncSession, event_id: uuid.UUID) -> Event:
    result = await db.execute(select(Event).where(Event.id == event_id).execution_options(populate_existing=True))
    event = result.scalar_one_or_none()
    if event is None:
        raise NotFoundError("Event not found")
    return event


def can_see_private_event_data(event: Event, user: User | None) -> bool:
    return user is not None and (user.role == Role.ADMIN.value or event.organizer_id == user.id)


async def get_visible_event(db: AsyncSession, event_id: uuid.UUID, viewer: User | None) -> Event:
    event = await get_event(db, event_id)
    if event.status not in PUBLIC_EVENT_STATUSES and not can_see_private_event_data(event, viewer):
        raise NotFoundError("Event not found")  # do not reveal that unpublished events exist
    return event


def ensure_owner(event: Event, user: User) -> None:
    """Resource-ownership check (IDOR protection): organizers only touch their own events."""
    if event.organizer_id != user.id:
        raise ForbiddenError("You do not have access to this event")


async def confirmed_registrants(db: AsyncSession, event_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    result = await db.execute(
        select(User.id, User.email)
        .join(Registration, Registration.user_id == User.id)
        .where(Registration.event_id == event_id, Registration.status == RegistrationStatus.CONFIRMED.value)
    )
    return [(row[0], row[1]) for row in result.all()]


# --------------------------------------------------------------------------- organizer actions
async def create_event(db: AsyncSession, organizer: User, data: EventCreate) -> Event:
    event = Event(
        organizer_id=organizer.id,
        title=data.title,
        description=data.description,
        category=data.category,
        cover_image=data.cover_image,
        start_datetime=data.start_datetime,
        end_datetime=data.end_datetime,
        registration_deadline=data.registration_deadline,
        venue=data.venue,
        location=data.location,
        event_type=data.event_type,
        capacity=data.capacity,
        ticket_type=data.ticket_type,
        ticket_price=Decimal("0.00") if data.ticket_type == "free" else data.ticket_price,
        status=EventStatus.DRAFT.value,
    )
    db.add(event)
    await db.commit()
    return await get_event(db, event.id)


async def update_event(
    db: AsyncSession, event: Event, user: User, data: EventUpdate, background: BackgroundTasks
) -> Event:
    ensure_owner(event, user)
    if event.status in (EventStatus.CANCELLED.value, EventStatus.COMPLETED.value):
        raise ConflictError("Cancelled or completed events can no longer be edited")

    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise BadRequestError("No fields to update")
    for field in _REQUIRED_UPDATE_FIELDS & changes.keys():
        if changes[field] is None:
            raise BadRequestError(f"{field} cannot be null")

    merged = {
        name: changes.get(name, getattr(event, name))
        for name in (
            "start_datetime", "end_datetime", "registration_deadline", "event_type", "venue",
            "capacity", "ticket_type", "ticket_price",
        )
    }
    if merged["ticket_type"] == "free":
        merged["ticket_price"] = Decimal("0.00")
        changes["ticket_price"] = Decimal("0.00")

    problem = find_event_rule_violation(
        start=merged["start_datetime"],
        end=merged["end_datetime"],
        deadline=merged["registration_deadline"],
        event_type=merged["event_type"],
        venue=merged["venue"],
        capacity=merged["capacity"],
        ticket_type=merged["ticket_type"],
        price=Decimal(merged["ticket_price"]),
        registered_count=event.registered_count,
        now=utcnow() if "start_datetime" in changes else None,
    )
    if problem:
        raise BadRequestError(problem)

    significant = any(changes.get(name) != getattr(event, name) for name in _SIGNIFICANT_FIELDS if name in changes)
    for name, value in changes.items():
        setattr(event, name, value)

    if significant and event.status == EventStatus.PUBLISHED.value:
        notify_many(
            db,
            await confirmed_registrants(db, event.id),
            kind=NotificationType.EVENT_UPDATED.value,
            title=f"Event updated: {event.title}",
            message=f"The organizer changed details (date, time or location) of '{event.title}'. Please review the event page.",
            event_id=event.id,
            background=background,
        )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("The event changed while you were editing it (e.g. new registrations). Please retry.") from None
    return await get_event(db, event.id)


async def submit_for_review(db: AsyncSession, event: Event, user: User) -> Event:
    ensure_owner(event, user)
    if event.status not in (EventStatus.DRAFT.value, EventStatus.REJECTED.value):
        raise ConflictError(f"Only draft or rejected events can be submitted (current status: {event.status})")
    if event.start_datetime <= utcnow():
        raise BadRequestError("An event that has already started cannot be submitted")
    event.status = EventStatus.PENDING.value
    event.rejection_reason = None
    await db.commit()
    return await get_event(db, event.id)


async def delete_or_cancel_event(
    db: AsyncSession, event: Event, user: User, background: BackgroundTasks
) -> str:
    """DELETE semantics by status. Returns 'deleted' or 'cancelled'.

    * never-published events with no registrations are removed;
    * everything else (published, or has registration history) is cancelled so records are preserved
      and registrants are notified;
    * completed events are immutable history.
    """
    ensure_owner(event, user)
    if event.status == EventStatus.COMPLETED.value:
        raise ConflictError("Completed events cannot be deleted")
    if event.status == EventStatus.CANCELLED.value:
        raise ConflictError("This event is already cancelled")

    registration_rows = (
        await db.execute(select(func.count()).select_from(Registration).where(Registration.event_id == event.id))
    ).scalar_one()
    removable = event.status in (
        EventStatus.DRAFT.value, EventStatus.PENDING.value, EventStatus.REJECTED.value, EventStatus.APPROVED.value
    )
    if removable and registration_rows == 0:
        await db.execute(delete(Event).where(Event.id == event.id))
        await db.commit()
        return "deleted"

    await cancel_event(db, event, user, background)
    return "cancelled"


async def cancel_event(db: AsyncSession, event: Event, user: User, background: BackgroundTasks) -> Event:
    ensure_owner(event, user)
    if event.status in (EventStatus.CANCELLED.value, EventStatus.COMPLETED.value):
        raise ConflictError(f"A {event.status} event cannot be cancelled")
    if event.end_datetime <= utcnow():
        raise ConflictError("An event that has already ended cannot be cancelled")

    now = utcnow()
    registrants = await confirmed_registrants(db, event.id)
    await db.execute(
        update(Ticket)
        .where(Ticket.event_id == event.id, Ticket.status == TicketStatus.VALID.value)
        .values(status=TicketStatus.CANCELLED.value)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(Registration)
        .where(Registration.event_id == event.id, Registration.status == RegistrationStatus.CONFIRMED.value)
        .values(status=RegistrationStatus.CANCELLED.value, cancelled_at=now, cancelled_by_id=user.id)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(Event)
        .where(Event.id == event.id)
        .values(status=EventStatus.CANCELLED.value, registered_count=0)
        .execution_options(synchronize_session=False)
    )
    notify_many(
        db,
        registrants,
        kind=NotificationType.EVENT_CANCELLED.value,
        title=f"Event cancelled: {event.title}",
        message=f"We're sorry — '{event.title}' has been cancelled by the organizer. Your registration was released.",
        event_id=event.id,
        background=background,
    )
    await db.commit()
    return await get_event(db, event.id)


# --------------------------------------------------------------------------- admin moderation
_TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "approve": ({EventStatus.PENDING.value}, EventStatus.APPROVED.value),
    "reject": ({EventStatus.PENDING.value}, EventStatus.REJECTED.value),
    "publish": ({EventStatus.APPROVED.value, EventStatus.PENDING.value}, EventStatus.PUBLISHED.value),
    "unpublish": ({EventStatus.PUBLISHED.value}, EventStatus.APPROVED.value),
}


async def moderate_event(
    db: AsyncSession,
    event: Event,
    action: str,
    admin: User,
    *,
    reason: str | None,
    request: Request | None,
    background: BackgroundTasks,
) -> Event:
    allowed_from, target = _TRANSITIONS[action]
    if event.status not in allowed_from:
        raise ConflictError(f"Cannot {action} an event whose status is '{event.status}'")
    now = utcnow()
    if action in ("approve", "publish") and event.end_datetime <= now:
        raise ConflictError("This event has already ended")

    values: dict[str, Any] = {"status": target}
    if action == "reject":
        values["rejection_reason"] = reason
    elif action in ("approve", "publish"):
        values["rejection_reason"] = None
    if action == "publish":
        values["published_at"] = now

    # Conditional update: if two admins act at once, only one transition succeeds.
    claimed = await db.execute(
        update(Event)
        .where(Event.id == event.id, Event.status.in_(list(allowed_from)))
        .values(**values)
        .execution_options(synchronize_session=False)
        .returning(Event.id)
    )
    if claimed.scalar_one_or_none() is None:
        await db.rollback()
        raise ConflictError("The event status changed in the meantime. Please reload and retry.")

    record_audit(
        db,
        actor=admin,
        action=f"event.{action}",
        target_type="event",
        target_id=event.id,
        details={"from": event.status, "to": target, "reason": reason},
        request=request,
    )
    organizer = event.organizer
    messages = {
        "approve": (NotificationType.EVENT_APPROVED, "Event approved", f"Your event '{event.title}' was approved."),
        "reject": (
            NotificationType.EVENT_REJECTED,
            "Event rejected",
            f"Your event '{event.title}' was rejected. Reason: {reason or 'not specified'}.",
        ),
        "publish": (NotificationType.EVENT_PUBLISHED, "Event published", f"Your event '{event.title}' is now live."),
        "unpublish": (
            NotificationType.EVENT_UPDATED, "Event unpublished", f"Your event '{event.title}' was unpublished by an administrator."
        ),
    }
    kind, title, message = messages[action]
    notify(
        db, user_id=organizer.id, email=organizer.email, kind=kind.value, title=title, message=message,
        event_id=event.id, background=background,
    )
    await db.commit()
    return await get_event(db, event.id)


async def set_featured(db: AsyncSession, event: Event, admin: User, featured: bool, request: Request | None) -> Event:
    if featured and event.status != EventStatus.PUBLISHED.value:
        raise ConflictError("Only published events can be featured")
    await db.execute(
        update(Event).where(Event.id == event.id).values(is_featured=featured).execution_options(synchronize_session=False)
    )
    record_audit(
        db, actor=admin, action="event.feature" if featured else "event.unfeature", target_type="event",
        target_id=event.id, request=request,
    )
    await db.commit()
    return await get_event(db, event.id)


# --------------------------------------------------------------------------- reports
async def create_report(db: AsyncSession, event: Event, reporter: User, data: EventReportCreate) -> EventReport:
    if event.status not in PUBLIC_EVENT_STATUSES:
        raise NotFoundError("Event not found")
    report = EventReport(event_id=event.id, reporter_id=reporter.id, reason=data.reason, details=data.details)
    db.add(report)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("You have already reported this event") from None
    return await get_report(db, report.id)


async def get_report(db: AsyncSession, report_id: uuid.UUID) -> EventReport:
    result = await db.execute(
        select(EventReport).where(EventReport.id == report_id).execution_options(populate_existing=True)
    )
    report = result.scalar_one_or_none()
    if report is None:
        raise NotFoundError("Report not found")
    return report


async def resolve_report(
    db: AsyncSession, report: EventReport, admin: User, status: str, note: str | None, request: Request | None
) -> EventReport:
    if report.status != ReportStatus.OPEN.value:
        raise ConflictError("This report has already been handled")
    report.status = status
    report.resolution_note = note
    report.resolved_by_id = admin.id
    report.resolved_at = utcnow()
    record_audit(
        db, actor=admin, action=f"report.{status}", target_type="event_report", target_id=report.id,
        details={"event_id": str(report.event_id)}, request=request,
    )
    await db.commit()
    return await get_report(db, report.id)
