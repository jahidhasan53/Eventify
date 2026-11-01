"""Organizer analytics. Every query is scoped to the authenticated organizer's own events."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import escape_like, utcnow
from app.models.enums import EventStatus, RegistrationStatus, TicketStatus
from app.models.event import Event
from app.models.registration import Registration
from app.models.review import Review
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.organizer import (
    EventStatisticsOut,
    OrganizerDashboardOut,
    OrganizerProfileOut,
    RecentRegistrationOut,
)
from app.schemas.registration import AttendeeOut
from app.schemas.user import UserOut

CONFIRMED = RegistrationStatus.CONFIRMED.value


def _pct(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


async def _totals(db: AsyncSession, organizer_id: uuid.UUID) -> tuple[int, int, int]:
    """(total events, active registrations, checked-in attendees) for one organizer."""
    events = (
        await db.execute(select(func.count()).select_from(Event).where(Event.organizer_id == organizer_id))
    ).scalar_one()
    registrations = (
        await db.execute(
            select(func.count())
            .select_from(Registration)
            .join(Event, Event.id == Registration.event_id)
            .where(Event.organizer_id == organizer_id, Registration.status == CONFIRMED)
        )
    ).scalar_one()
    attendees = (
        await db.execute(
            select(func.count())
            .select_from(Ticket)
            .join(Event, Event.id == Ticket.event_id)
            .where(Event.organizer_id == organizer_id, Ticket.status == TicketStatus.USED.value)
        )
    ).scalar_one()
    return events, registrations, attendees


async def dashboard(db: AsyncSession, organizer: User) -> OrganizerDashboardOut:
    now = utcnow()
    total_events, registrations, attendees = await _totals(db, organizer.id)
    published = (
        await db.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.organizer_id == organizer.id, Event.status == EventStatus.PUBLISHED.value)
        )
    ).scalar_one()
    upcoming = (
        await db.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.organizer_id == organizer.id,
                Event.status == EventStatus.PUBLISHED.value,
                Event.start_datetime > now,
            )
        )
    ).scalar_one()
    recent_rows = (
        await db.execute(
            select(
                Registration.id,
                Registration.reference,
                Registration.status,
                Registration.created_at,
                Event.id,
                Event.title,
                User.full_name,
            )
            .join(Event, Event.id == Registration.event_id)
            .join(User, User.id == Registration.user_id)
            .where(Event.organizer_id == organizer.id)
            .order_by(Registration.created_at.desc(), Registration.id)
            .limit(10)
        )
    ).all()
    recent = [
        RecentRegistrationOut(
            registration_id=r[0], reference=r[1], status=r[2], registered_at=r[3],
            event_id=r[4], event_title=r[5], attendee_name=r[6],
        )
        for r in recent_rows
    ]
    return OrganizerDashboardOut(
        total_events=total_events,
        published_events=published,
        upcoming_events=upcoming,
        total_registrations=registrations,
        total_attendees=attendees,
        attendance_percentage=_pct(attendees, registrations),
        recent_registrations=recent,
    )


async def profile(db: AsyncSession, organizer: User) -> OrganizerProfileOut:
    total_events, registrations, attendees = await _totals(db, organizer.id)
    return OrganizerProfileOut(
        user=UserOut.model_validate(organizer),
        total_events=total_events,
        total_registrations=registrations,
        total_attendees=attendees,
    )


async def event_statistics(db: AsyncSession, event: Event) -> EventStatisticsOut:
    """Caller must already have verified that `event` belongs to the requesting organizer."""
    active = (
        await db.execute(
            select(func.count())
            .select_from(Registration)
            .where(Registration.event_id == event.id, Registration.status == CONFIRMED)
        )
    ).scalar_one()
    cancelled = (
        await db.execute(
            select(func.count())
            .select_from(Registration)
            .where(Registration.event_id == event.id, Registration.status == RegistrationStatus.CANCELLED.value)
        )
    ).scalar_one()
    checked_in = (
        await db.execute(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.event_id == event.id, Ticket.status == TicketStatus.USED.value)
        )
    ).scalar_one()
    revenue = (
        await db.execute(
            select(func.coalesce(func.sum(Registration.amount), 0)).where(
                Registration.event_id == event.id, Registration.status == CONFIRMED
            )
        )
    ).scalar_one()
    review_count, average = (
        await db.execute(select(func.count(Review.id), func.avg(Review.rating)).where(Review.event_id == event.id))
    ).one()
    return EventStatisticsOut(
        event_id=event.id,
        title=event.title,
        status=event.status,
        capacity=event.capacity,
        registered_count=event.registered_count,
        seats_remaining=max(event.capacity - event.registered_count, 0),
        active_registrations=active,
        cancelled_registrations=cancelled,
        checked_in=checked_in,
        attendance_percentage=_pct(checked_in, active),
        revenue=float(revenue),
        review_count=review_count,
        average_rating=round(float(average), 2) if average is not None else None,
    )


async def list_attendees(
    db: AsyncSession, event_id: uuid.UUID, *, status: str | None, q: str | None, offset: int, limit: int
) -> tuple[list[AttendeeOut], int]:
    clauses = [Registration.event_id == event_id]
    if status:
        clauses.append(Registration.status == status)
    if q:
        pattern = f"%{escape_like(q)}%"
        clauses.append(User.full_name.ilike(pattern, escape="\\") | User.email.ilike(pattern, escape="\\"))

    total = (
        await db.execute(
            select(func.count())
            .select_from(Registration)
            .join(User, User.id == Registration.user_id)
            .where(*clauses)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                Registration.id,
                Registration.reference,
                Registration.status,
                Registration.created_at,
                User.id,
                User.full_name,
                User.email,
                Ticket.id,
                Ticket.status,
                Ticket.checked_in_at,
            )
            .join(User, User.id == Registration.user_id)
            .outerjoin(Ticket, Ticket.registration_id == Registration.id)
            .where(*clauses)
            .order_by(Registration.created_at.desc(), Registration.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    attendees = [
        AttendeeOut(
            registration_id=r[0], reference=r[1], status=r[2], registered_at=r[3], user_id=r[4],
            full_name=r[5], email=r[6], ticket_id=r[7], ticket_status=r[8], checked_in_at=r[9],
        )
        for r in rows
    ]
    return attendees, total
