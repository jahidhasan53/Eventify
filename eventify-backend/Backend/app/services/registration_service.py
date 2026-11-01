"""Event registration with overbooking protection.

Concurrency design (all inside ONE transaction):

1. Claim a seat with a single conditional UPDATE
       UPDATE events SET registered_count = registered_count + 1
       WHERE id = :id AND status = 'published' AND registered_count < capacity AND <deadline open>
   PostgreSQL row-locks the event for the UPDATE, so concurrent claims are serialised and each one
   re-checks the WHERE clause against the latest committed value: seats can never exceed capacity.
2. Insert the registration. A partial UNIQUE index (user_id, event_id WHERE status='confirmed')
   rejects duplicate active registrations; on violation the whole transaction (including the seat
   claim) is rolled back.
3. Insert the ticket and commit.

CHECK (registered_count <= capacity) in the database is a final backstop.
"""

import uuid
from decimal import Decimal

from fastapi import BackgroundTasks
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.security import generate_ticket_nonce
from app.core.utils import generate_registration_reference, utcnow
from app.models.enums import EventStatus, NotificationType, RegistrationStatus, Role, TicketStatus
from app.models.event import Event
from app.models.registration import Registration
from app.models.ticket import Ticket
from app.models.user import User
from app.services.event_service import get_event
from app.services.notification_service import notify


async def load_registration(db: AsyncSession, registration_id: uuid.UUID) -> Registration:
    result = await db.execute(
        select(Registration).where(Registration.id == registration_id).execution_options(populate_existing=True)
    )
    registration = result.scalar_one_or_none()
    if registration is None:
        raise NotFoundError("Registration not found")
    return registration


async def register_for_event(
    db: AsyncSession, user: User, event_id: uuid.UUID, background: BackgroundTasks
) -> Registration:
    now = utcnow()
    event = await get_event(db, event_id)  # 404 if it does not exist

    if event.organizer_id == user.id:
        raise ForbiddenError("Organizers cannot register for their own events")
    if event.status != EventStatus.PUBLISHED.value:
        raise ConflictError("This event is not open for registration")

    duplicate = await db.execute(
        select(Registration.id).where(
            Registration.user_id == user.id,
            Registration.event_id == event.id,
            Registration.status == RegistrationStatus.CONFIRMED.value,
        )
    )
    if duplicate.first() is not None:
        raise ConflictError("You are already registered for this event")

    # Atomic seat claim (see module docstring). updated_at is set explicitly to itself so that a
    # seat change does not look like an edit of the event.
    claim = await db.execute(
        update(Event)
        .where(
            Event.id == event.id,
            Event.status == EventStatus.PUBLISHED.value,
            Event.registered_count < Event.capacity,
            Event.registration_deadline > now,
            Event.end_datetime > now,
        )
        .values(registered_count=Event.registered_count + 1, updated_at=Event.updated_at)
        .execution_options(synchronize_session=False)
        .returning(Event.id)
    )
    if claim.scalar_one_or_none() is None:
        # Nothing was modified, so no rollback is needed; work out the most helpful reason.
        fresh = await get_event(db, event.id)
        if fresh.status != EventStatus.PUBLISHED.value:
            raise ConflictError("This event is not open for registration")
        if fresh.registration_deadline <= now or fresh.end_datetime <= now:
            raise ConflictError("Registration is closed")
        raise ConflictError("This event is full")

    registration = Registration(
        user_id=user.id,
        event_id=event.id,
        reference=generate_registration_reference(),
        amount=Decimal(event.ticket_price),
    )
    db.add(registration)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()  # also undoes the seat claim above
        raise ConflictError("You are already registered for this event") from None

    db.add(
        Ticket(
            registration_id=registration.id,
            event_id=event.id,
            user_id=user.id,
            token_nonce=generate_ticket_nonce(),
        )
    )
    notify(
        db,
        user_id=user.id,
        email=user.email,
        kind=NotificationType.REGISTRATION_CONFIRMED.value,
        title=f"Registration confirmed: {event.title}",
        message=(
            f"You're registered for '{event.title}' on {event.start_datetime:%Y-%m-%d %H:%M} UTC. "
            f"Reference: {registration.reference}. Your QR ticket is available in your account."
        ),
        event_id=event.id,
        background=background,
    )
    registration_id = registration.id
    await db.commit()
    return await load_registration(db, registration_id)


async def cancel_registration(
    db: AsyncSession,
    registration_id: uuid.UUID,
    actor: User,
    background: BackgroundTasks,
    *,
    as_organizer: bool = False,
) -> Registration:
    """Cancel a registration and release its seat.

    Attendees may cancel only their own registration (anything else is reported as 404, so ids of
    other people's registrations are not confirmable). Organizers may remove attendees from their
    own events (`as_organizer=True`).
    """
    registration = await load_registration(db, registration_id)
    event = registration.event
    now = utcnow()

    if as_organizer:
        if event.organizer_id != actor.id:
            raise ForbiddenError("You do not have access to this event")
    elif registration.user_id != actor.id:
        raise NotFoundError("Registration not found")

    if registration.status != RegistrationStatus.CONFIRMED.value:
        raise ConflictError("This registration is already cancelled")
    if not as_organizer and event.start_datetime <= now:
        raise ConflictError("Registrations cannot be cancelled after the event has started")

    attendee_id, attendee_email = registration.user_id, None
    attendee = await db.get(User, attendee_id)
    if attendee is not None:
        attendee_email = attendee.email
    event_id, event_title = event.id, event.title

    # 1) Invalidate the ticket (fails if it was already used for check-in).
    ticket_claim = await db.execute(
        update(Ticket)
        .where(Ticket.registration_id == registration_id, Ticket.status == TicketStatus.VALID.value)
        .values(status=TicketStatus.CANCELLED.value)
        .execution_options(synchronize_session=False)
        .returning(Ticket.id)
    )
    if ticket_claim.scalar_one_or_none() is None:
        # Either a concurrent request already cancelled it, or the ticket was used for check-in.
        await db.rollback()
        current = await load_registration(db, registration_id)
        if current.status == RegistrationStatus.CANCELLED.value:
            raise ConflictError("This registration is already cancelled")
        raise ConflictError("This ticket has already been used, so the registration cannot be cancelled")

    # 2) Atomically flip the registration (guards against double-cancel races).
    reg_claim = await db.execute(
        update(Registration)
        .where(Registration.id == registration_id, Registration.status == RegistrationStatus.CONFIRMED.value)
        .values(
            status=RegistrationStatus.CANCELLED.value,
            cancelled_at=now,
            cancelled_by_id=actor.id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
        .returning(Registration.id)
    )
    if reg_claim.scalar_one_or_none() is None:
        await db.rollback()
        raise ConflictError("This registration is already cancelled")

    # 3) Release the seat (never below zero).
    await db.execute(
        update(Event)
        .where(Event.id == event_id, Event.registered_count > 0)
        .values(registered_count=Event.registered_count - 1, updated_at=Event.updated_at)
        .execution_options(synchronize_session=False)
    )

    notify(
        db,
        user_id=attendee_id,
        email=attendee_email,
        kind=NotificationType.REGISTRATION_CANCELLED.value,
        title=f"Registration cancelled: {event_title}",
        message=(
            f"Your registration for '{event_title}' was cancelled"
            + (" by the organizer." if as_organizer else ".")
        ),
        event_id=event_id,
        background=background,
    )
    await db.commit()
    return await load_registration(db, registration_id)


async def list_my_registrations(
    db: AsyncSession, user: User, *, status: str | None, offset: int, limit: int
) -> tuple[list[Registration], int]:
    clauses = [Registration.user_id == user.id]
    if status:
        clauses.append(Registration.status == status)
    total = (await db.execute(select(func.count()).select_from(Registration).where(*clauses))).scalar_one()
    result = await db.execute(
        select(Registration)
        .where(*clauses)
        .order_by(Registration.created_at.desc(), Registration.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def get_registration_for_user(db: AsyncSession, registration_id: uuid.UUID, user: User) -> Registration:
    registration = await load_registration(db, registration_id)
    if registration.user_id != user.id and user.role != Role.ADMIN.value:
        raise NotFoundError("Registration not found")
    return registration
