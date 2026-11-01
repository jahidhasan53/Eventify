import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.api.v1.endpoints.events import Filters
from app.core.permissions import Permission
from app.database.connection import DbSession
from app.dependencies.authorization import require_permission
from app.dependencies.pagination import PageParams
from app.models.event import Event
from app.models.user import User
from app.schemas.common import Envelope, Page, ok, paginated
from app.schemas.event import EventOut, to_event_out
from app.schemas.organizer import EventStatisticsOut, OrganizerDashboardOut, OrganizerProfileOut
from app.schemas.registration import AttendeeOut, RegistrationOut
from app.services import event_service, organizer_service, registration_service

router = APIRouter(prefix="/organizer", tags=["Organizer"])

Analyst = Annotated[User, Depends(require_permission(Permission.ORGANIZER_ANALYTICS))]
AttendeeManager = Annotated[User, Depends(require_permission(Permission.ATTENDEE_MANAGE_OWN_EVENT))]


async def _own_event(db, event_id: uuid.UUID, user: User) -> Event:
    event = await event_service.get_event(db, event_id)
    event_service.ensure_owner(event, user)  # IDOR protection: 403 for other organizers' events
    return event


@router.get("/dashboard", response_model=Envelope[OrganizerDashboardOut], summary="Totals and recent registrations of own events")
async def dashboard(user: Analyst, db: DbSession):
    return ok(await organizer_service.dashboard(db, user))


@router.get("/profile", response_model=Envelope[OrganizerProfileOut], summary="Organizer profile with headline numbers")
async def profile(user: Analyst, db: DbSession):
    return ok(await organizer_service.profile(db, user))


@router.get("/events", response_model=Envelope[Page[EventOut]], summary="Own events in every status")
async def my_events(
    user: Analyst,
    db: DbSession,
    page: PageParams,
    filters: Filters,
    status: Annotated[
        Literal["draft", "pending", "approved", "rejected", "published", "cancelled", "completed"] | None, Query()
    ] = None,
):
    clauses = event_service.common_clauses(filters) + [Event.organizer_id == user.id]
    if status:
        clauses.append(Event.status == status)
    events, total = await event_service.list_events(
        db, clauses, sort_by=filters.sort_by, order=filters.order, offset=page.offset, limit=page.page_size
    )
    items = [to_event_out(e, include_private=True) for e in events]
    return ok(paginated(items, total, page.page, page.page_size))


@router.get(
    "/events/{event_id}/attendees",
    response_model=Envelope[Page[AttendeeOut]],
    summary="Attendees of one of your events",
)
async def event_attendees(
    event_id: uuid.UUID,
    user: AttendeeManager,
    db: DbSession,
    page: PageParams,
    status: Annotated[Literal["confirmed", "cancelled"] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100, description="Search attendee name or e-mail")] = None,
):
    event = await _own_event(db, event_id, user)
    attendees, total = await organizer_service.list_attendees(
        db, event.id, status=status, q=q.strip() if q else None, offset=page.offset, limit=page.page_size
    )
    return ok(paginated(attendees, total, page.page, page.page_size))


@router.get(
    "/events/{event_id}/statistics",
    response_model=Envelope[EventStatisticsOut],
    summary="Registration / attendance analytics of one of your events",
)
async def event_statistics(event_id: uuid.UUID, user: Analyst, db: DbSession):
    event = await _own_event(db, event_id, user)
    return ok(await organizer_service.event_statistics(db, event))


@router.delete(
    "/registrations/{registration_id}",
    response_model=Envelope[RegistrationOut],
    summary="Remove an attendee from one of your events (cancels the registration and releases the seat)",
)
async def remove_attendee(registration_id: uuid.UUID, user: AttendeeManager, db: DbSession, background: BackgroundTasks):
    registration = await registration_service.cancel_registration(
        db, registration_id, user, background, as_organizer=True
    )
    return ok(RegistrationOut.model_validate(registration), "Attendee removed")
