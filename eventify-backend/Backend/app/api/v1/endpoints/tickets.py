import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from app.core.exceptions import ConflictError, NotFoundError
from app.core.permissions import Permission
from app.core.rate_limit import RateLimit
from app.database.connection import DbSession
from app.dependencies.authorization import require_permission
from app.dependencies.pagination import PageParams
from app.models.enums import TicketStatus
from app.models.user import User
from app.schemas.common import Envelope, Page, ok, paginated
from app.schemas.ticket import TicketDetailOut, TicketOut, TicketTokenRequest, TicketVerifyOut
from app.services import ticket_service

router = APIRouter(prefix="/tickets", tags=["Tickets"])

TicketHolder = Annotated[User, Depends(require_permission(Permission.TICKET_VIEW_OWN))]
Verifier = Annotated[User, Depends(require_permission(Permission.TICKET_VERIFY))]

# Stops token brute-forcing from a stolen organizer session / scanner abuse.
_verify_limit = RateLimit("ticket-verify", 120)


async def _own_ticket(db, ticket_id: uuid.UUID, user: User):
    """The QR credential is only ever shown to the ticket's owner (even admins do not get it)."""
    ticket = await ticket_service.get_ticket_for_user(db, ticket_id, user)
    if ticket.user_id != user.id:
        raise NotFoundError("Ticket not found")
    return ticket


@router.get("/my", response_model=Envelope[Page[TicketOut]], summary="Own tickets")
async def my_tickets(
    user: TicketHolder,
    db: DbSession,
    page: PageParams,
    status: Annotated[Literal["valid", "used", "cancelled"] | None, Query()] = None,
):
    rows, total = await ticket_service.list_my_tickets(db, user, status=status, offset=page.offset, limit=page.page_size)
    items = [TicketOut.model_validate(t) for t in rows]
    return ok(paginated(items, total, page.page, page.page_size))


@router.post(
    "/verify",
    response_model=Envelope[TicketVerifyOut],
    summary="Check a scanned QR token (read-only; organizer of that event only)",
    dependencies=[Depends(_verify_limit)],
)
async def verify_ticket(body: TicketTokenRequest, user: Verifier, db: DbSession):
    # The token is read from the body (never the URL) so it does not end up in access logs.
    result = await ticket_service.verify_ticket(db, user, body.token)
    return ok(result, "Ticket is valid")


@router.get("/{ticket_id}", response_model=Envelope[TicketDetailOut], summary="One of your tickets, including the QR token")
async def get_ticket(ticket_id: uuid.UUID, user: TicketHolder, db: DbSession):
    ticket = await _own_ticket(db, ticket_id, user)
    detail = TicketDetailOut.model_validate({**TicketOut.model_validate(ticket).model_dump(), "qr_token": ticket_service.token_for(ticket)})
    return ok(detail)


@router.get(
    "/{ticket_id}/qr",
    summary="QR code image (PNG) for one of your tickets",
    responses={200: {"content": {"image/png": {}}, "description": "PNG image"}},
    response_class=Response,
)
async def get_ticket_qr(ticket_id: uuid.UUID, user: TicketHolder, db: DbSession):
    ticket = await _own_ticket(db, ticket_id, user)
    if ticket.status == TicketStatus.CANCELLED.value:
        raise ConflictError("This ticket has been cancelled")
    png = await ticket_service.render_qr_png(ticket_service.token_for(ticket))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})
