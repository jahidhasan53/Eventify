"""QR tickets: issuing happens in registration_service; this module renders, verifies and checks in.

Token model (see app.core.security): the QR holds only '<ticket-id-hex>.<HMAC-SHA256 signature>'.
It contains no personal data and is validated server-side on every use. Raw tokens are never
logged: they travel in request bodies (not URLs) and this module never writes them to any log.
"""

import io
import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import build_ticket_token, parse_ticket_token, verify_ticket_signature
from app.core.utils import utcnow
from app.models.enums import EventStatus, Role, TicketStatus
from app.models.event import Event
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.ticket import AttendanceRecordOut, CheckInOut, TicketVerifyOut


# --------------------------------------------------------------------------- reading
async def load_ticket(db: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True))
    ticket = result.scalar_one_or_none()
    if ticket is None:
        raise NotFoundError("Ticket not found")
    return ticket


async def get_ticket_for_user(db: AsyncSession, ticket_id: uuid.UUID, user: User) -> Ticket:
    ticket = await load_ticket(db, ticket_id)
    if ticket.user_id != user.id and user.role != Role.ADMIN.value:
        raise NotFoundError("Ticket not found")  # same answer as "does not exist": no ID probing
    return ticket


async def list_my_tickets(
    db: AsyncSession, user: User, *, status: str | None, offset: int, limit: int
) -> tuple[list[Ticket], int]:
    clauses = [Ticket.user_id == user.id]
    if status:
        clauses.append(Ticket.status == status)
    total = (await db.execute(select(func.count()).select_from(Ticket).where(*clauses))).scalar_one()
    result = await db.execute(
        select(Ticket).where(*clauses).order_by(Ticket.issued_at.desc(), Ticket.id).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), total


def token_for(ticket: Ticket) -> str:
    return build_ticket_token(ticket.id, ticket.token_nonce)


def _render_qr_png_sync(data: str) -> bytes:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=8, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def render_qr_png(token: str) -> bytes:
    return await run_in_threadpool(_render_qr_png_sync, token)


# --------------------------------------------------------------------------- verification
async def resolve_token(db: AsyncSession, token: str) -> Ticket:
    """Map a presented token to its ticket, verifying the HMAC signature in constant time."""
    parsed = parse_ticket_token(token)
    if parsed is None:
        raise BadRequestError("Invalid ticket")
    ticket_id, signature = parsed
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True))
    ticket = result.scalar_one_or_none()
    if ticket is None or not verify_ticket_signature(ticket.id, ticket.token_nonce, signature):
        raise BadRequestError("Invalid ticket")
    return ticket


def _ensure_organizer_of(event: Event, organizer: User) -> None:
    if organizer.role != Role.ORGANIZER.value or event.organizer_id != organizer.id:
        raise ForbiddenError("You are not authorized to verify tickets for this event")


def _ensure_usable(ticket: Ticket, now: datetime) -> None:
    event = ticket.event
    if ticket.status == TicketStatus.USED.value:
        when = ticket.checked_in_at.isoformat() if ticket.checked_in_at else None
        raise ConflictError("Ticket has already been used", detail={"checked_in_at": when})
    if ticket.status == TicketStatus.CANCELLED.value:
        raise ConflictError("Ticket has been cancelled")
    if event.status == EventStatus.CANCELLED.value:
        raise ConflictError("The event has been cancelled")
    if event.status == EventStatus.COMPLETED.value or event.end_datetime <= now:
        raise ConflictError("Ticket has expired (the event has ended)")
    if event.status != EventStatus.PUBLISHED.value:
        raise ConflictError("The event is not active")


async def _attendee_name(db: AsyncSession, user_id: uuid.UUID) -> str:
    return (await db.execute(select(User.full_name).where(User.id == user_id))).scalar_one_or_none() or "Unknown"


async def verify_ticket(db: AsyncSession, organizer: User, token: str) -> TicketVerifyOut:
    """Read-only validity check for the organizer of the ticket's event."""
    ticket = await resolve_token(db, token)
    _ensure_organizer_of(ticket.event, organizer)
    _ensure_usable(ticket, utcnow())
    return TicketVerifyOut(
        ticket_id=ticket.id,
        event_id=ticket.event_id,
        event_title=ticket.event.title,
        attendee_name=await _attendee_name(db, ticket.user_id),
        status=ticket.status,
    )


async def check_in(db: AsyncSession, organizer: User, token: str) -> CheckInOut:
    ticket = await resolve_token(db, token)
    _ensure_organizer_of(ticket.event, organizer)
    now = utcnow()
    _ensure_usable(ticket, now)

    ticket_id, event_id, attendee_id = ticket.id, ticket.event_id, ticket.user_id
    # Atomic claim: only ONE concurrent request can flip valid -> used.
    claim = await db.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == TicketStatus.VALID.value)
        .values(status=TicketStatus.USED.value, checked_in_at=now, checked_in_by_id=organizer.id)
        .execution_options(synchronize_session=False)
        .returning(Ticket.checked_in_at)
    )
    row = claim.first()
    if row is None:
        fresh = await load_ticket(db, ticket_id)
        _ensure_usable(fresh, utcnow())  # raises the precise reason (already used / cancelled / ...)
        raise ConflictError("Ticket could not be checked in")
    checked_in_at = row[0]
    organizer_id = organizer.id
    attendee_name = await _attendee_name(db, attendee_id)
    await db.commit()
    return CheckInOut(
        ticket_id=ticket_id,
        event_id=event_id,
        attendee_name=attendee_name,
        checked_in_at=checked_in_at,
        checked_in_by_id=organizer_id,
    )


# --------------------------------------------------------------------------- attendance listings
async def list_event_checkins(
    db: AsyncSession, event_id: uuid.UUID, *, offset: int, limit: int
) -> tuple[list[AttendanceRecordOut], int]:
    clauses = [Ticket.event_id == event_id, Ticket.status == TicketStatus.USED.value]
    total = (await db.execute(select(func.count()).select_from(Ticket).where(*clauses))).scalar_one()
    result = await db.execute(
        select(Ticket.id, Ticket.user_id, User.full_name, Ticket.checked_in_at, Ticket.checked_in_by_id)
        .join(User, User.id == Ticket.user_id)
        .where(*clauses)
        .order_by(Ticket.checked_in_at.desc(), Ticket.id)
        .offset(offset)
        .limit(limit)
    )
    records = [
        AttendanceRecordOut(
            ticket_id=row[0], attendee_id=row[1], attendee_name=row[2], checked_in_at=row[3], checked_in_by_id=row[4]
        )
        for row in result.all()
    ]
    return records, total


async def list_my_attendance(db: AsyncSession, user: User, *, offset: int, limit: int) -> tuple[list[Ticket], int]:
    clauses = [Ticket.user_id == user.id, Ticket.status == TicketStatus.USED.value]
    total = (await db.execute(select(func.count()).select_from(Ticket).where(*clauses))).scalar_one()
    result = await db.execute(
        select(Ticket).where(*clauses).order_by(Ticket.checked_in_at.desc(), Ticket.id).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), total
