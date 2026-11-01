import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.core.exceptions import BadRequestError
from app.core.permissions import Permission
from app.core.utils import utcnow
from app.database.connection import DbSession
from app.dependencies.authentication import OptionalUser
from app.dependencies.authorization import require_permission
from app.dependencies.pagination import PageParams
from app.models.event import Event
from app.models.user import User
from app.schemas.common import Envelope, Page, ok, paginated
from app.schemas.event import (
    EventCreate,
    EventOut,
    EventReportCreate,
    EventReportOut,
    EventUpdate,
    to_event_out,
)
from app.services import event_service
from app.services.event_service import EventFilters

router = APIRouter(prefix="/events", tags=["Events"])

EventCreator = Annotated[User, Depends(require_permission(Permission.EVENT_CREATE))]
EventManager = Annotated[User, Depends(require_permission(Permission.EVENT_MANAGE_OWN))]
Reporter = Annotated[User, Depends(require_permission(Permission.EVENT_REPORT))]


def get_event_filters(
    q: Annotated[str | None, Query(max_length=100, description="Search in the event title")] = None,
    category: Annotated[str | None, Query(max_length=80)] = None,
    location: Annotated[str | None, Query(max_length=100, description="Matches location or venue")] = None,
    event_type: Literal["online", "offline"] | None = None,
    ticket_type: Annotated[Literal["free", "paid"] | None, Query(description="free or paid")] = None,
    date_from: Annotated[date | None, Query(description="Events starting on/after this date (UTC)")] = None,
    date_to: Annotated[date | None, Query(description="Events starting on/before this date (UTC)")] = None,
    include_past: Annotated[bool, Query(description="Also include completed events")] = False,
    sort_by: Literal["start_datetime", "created_at", "title", "ticket_price", "registered_count"] = "start_datetime",
    order: Literal["asc", "desc"] = "asc",
) -> EventFilters:
    if date_from and date_to and date_from > date_to:
        raise BadRequestError("date_from must not be after date_to")
    return EventFilters(
        q=q.strip() if q else None,
        category=category.strip() if category else None,
        location=location.strip() if location else None,
        event_type=event_type,
        ticket_type=ticket_type,
        date_from=date_from,
        date_to=date_to,
        include_past=include_past,
        sort_by=sort_by,
        order=order,
    )


Filters = Annotated[EventFilters, Depends(get_event_filters)]


async def _page(db, clauses, filters: EventFilters, page) -> dict:
    events, total = await event_service.list_events(
        db, clauses, sort_by=filters.sort_by, order=filters.order, offset=page.offset, limit=page.page_size
    )
    items = [to_event_out(event) for event in events]
    return paginated(items, total, page.page, page.page_size)


@router.post("/", status_code=201, response_model=Envelope[EventOut], summary="Create an event (organizers; starts as draft)")
async def create_event(body: EventCreate, user: EventCreator, db: DbSession):
    # organizer_id comes from the authenticated user; the body cannot carry it (extra fields are rejected).
    event = await event_service.create_event(db, user, body)
    return ok(to_event_out(event, include_private=True), "Event created successfully")


@router.get("/", response_model=Envelope[Page[EventOut]], summary="Browse published events (public)")
async def list_events(db: DbSession, page: PageParams, filters: Filters):
    return ok(await _page(db, event_service.public_clauses(filters), filters, page))


@router.get("/featured", response_model=Envelope[Page[EventOut]], summary="Featured upcoming events (public)")
async def featured_events(db: DbSession, page: PageParams, filters: Filters):
    clauses = event_service.public_clauses(filters) + [Event.is_featured.is_(True)]
    return ok(await _page(db, clauses, filters, page))


@router.get("/upcoming", response_model=Envelope[Page[EventOut]], summary="Upcoming published events (public)")
async def upcoming_events(db: DbSession, page: PageParams, filters: Filters):
    filters.include_past = False
    clauses = event_service.public_clauses(filters) + [Event.start_datetime > utcnow()]
    return ok(await _page(db, clauses, filters, page))


@router.get("/categories", response_model=Envelope[list[str]], summary="Distinct categories of public events")
async def categories(db: DbSession):
    return ok(await event_service.list_categories(db))


@router.get("/{event_id}", response_model=Envelope[EventOut], summary="Event details")
async def get_event(event_id: uuid.UUID, db: DbSession, viewer: OptionalUser):
    # Unpublished events are visible only to their organizer and admins (everyone else gets 404).
    event = await event_service.get_visible_event(db, event_id, viewer)
    private = event_service.can_see_private_event_data(event, viewer)
    return ok(to_event_out(event, include_private=private))


@router.put("/{event_id}", response_model=Envelope[EventOut], summary="Update own event")
async def update_event(event_id: uuid.UUID, body: EventUpdate, user: EventManager, db: DbSession, background: BackgroundTasks):
    event = await event_service.get_event(db, event_id)
    updated = await event_service.update_event(db, event, user, body, background)
    return ok(to_event_out(updated, include_private=True), "Event updated successfully")


@router.delete("/{event_id}", response_model=Envelope[dict], summary="Delete (never published, no registrations) or cancel own event")
async def delete_event(event_id: uuid.UUID, user: EventManager, db: DbSession, background: BackgroundTasks):
    event = await event_service.get_event(db, event_id)
    outcome = await event_service.delete_or_cancel_event(db, event, user, background)
    message = "Event deleted successfully" if outcome == "deleted" else "Event cancelled; registrants were notified"
    return ok({"result": outcome}, message)


@router.post("/{event_id}/submit", response_model=Envelope[EventOut], summary="Submit own draft/rejected event for admin review")
async def submit_event(event_id: uuid.UUID, user: EventManager, db: DbSession):
    event = await event_service.get_event(db, event_id)
    submitted = await event_service.submit_for_review(db, event, user)
    return ok(to_event_out(submitted, include_private=True), "Event submitted for review")


@router.post("/{event_id}/cancel", response_model=Envelope[EventOut], summary="Cancel own event and release all registrations")
async def cancel_event(event_id: uuid.UUID, user: EventManager, db: DbSession, background: BackgroundTasks):
    event = await event_service.get_event(db, event_id)
    cancelled = await event_service.cancel_event(db, event, user, background)
    return ok(to_event_out(cancelled, include_private=True), "Event cancelled; registrants were notified")


@router.post("/{event_id}/report", status_code=201, response_model=Envelope[EventReportOut], summary="Report an event to the moderators")
async def report_event(event_id: uuid.UUID, body: EventReportCreate, user: Reporter, db: DbSession):
    event = await event_service.get_event(db, event_id)
    report = await event_service.create_report(db, event, user, body)
    return ok(EventReportOut.model_validate(report), "Report submitted. Thank you")
