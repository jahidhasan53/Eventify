import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.exceptions import ForbiddenError
from app.core.permissions import Permission, has_permission
from app.core.rate_limit import RateLimit
from app.database.connection import DbSession
from app.dependencies.authentication import CurrentUser
from app.dependencies.authorization import require_permission
from app.dependencies.pagination import PageParams
from app.models.enums import Role
from app.models.user import User
from app.schemas.common import Envelope, Page, ok, paginated
from app.schemas.ticket import AttendanceRecordOut, CheckInOut, TicketOut, TicketTokenRequest
from app.services import event_service, ticket_service

router = APIRouter(prefix="/attendance", tags=["Attendance"])

CheckInUser = Annotated[User, Depends(require_permission(Permission.ATTENDANCE_CHECK_IN))]
MyAttendanceUser = Annotated[User, Depends(require_permission(Permission.ATTENDANCE_VIEW_OWN))]

_checkin_limit = RateLimit("check-in", 120)


@router.post(
    "/check-in",
    response_model=Envelope[CheckInOut],
    summary="Check an attendee in by QR token (organizer of that event only; atomic, single use)",
    dependencies=[Depends(_checkin_limit)],
)
async def check_in(body: TicketTokenRequest, user: CheckInUser, db: DbSession):
    result = await ticket_service.check_in(db, user, body.token)
    return ok(result, "Check-in successful")


@router.get("/my", response_model=Envelope[Page[TicketOut]], summary="Events you attended (checked-in tickets)")
async def my_attendance(user: MyAttendanceUser, db: DbSession, page: PageParams):
    tickets, total = await ticket_service.list_my_attendance(db, user, offset=page.offset, limit=page.page_size)
    items = [TicketOut.model_validate(t) for t in tickets]
    return ok(paginated(items, total, page.page, page.page_size))


@router.get(
    "/event/{event_id}",
    response_model=Envelope[Page[AttendanceRecordOut]],
    summary="Check-in records of an event (its organizer, or an admin)",
)
async def event_attendance(event_id: uuid.UUID, user: CurrentUser, db: DbSession, page: PageParams):
    event = await event_service.get_event(db, event_id)
    if user.role == Role.ADMIN.value:
        if not has_permission(user.role, Permission.ATTENDANCE_VIEW_ANY):  # pragma: no cover - matrix guard
            raise ForbiddenError()
    else:
        if not has_permission(user.role, Permission.ATTENDEE_MANAGE_OWN_EVENT):
            raise ForbiddenError()
        event_service.ensure_owner(event, user)  # organizers see only their own events
    records, total = await ticket_service.list_event_checkins(db, event.id, offset=page.offset, limit=page.page_size)
    return ok(paginated(records, total, page.page, page.page_size))
