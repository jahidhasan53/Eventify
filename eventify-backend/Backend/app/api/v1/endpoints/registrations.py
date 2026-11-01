import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.core.config import get_settings
from app.core.permissions import Permission
from app.core.rate_limit import RateLimit
from app.database.connection import DbSession
from app.dependencies.authentication import CurrentUser
from app.dependencies.authorization import require_permission
from app.dependencies.pagination import PageParams
from app.models.user import User
from app.schemas.common import Envelope, Page, ok, paginated
from app.schemas.registration import RegistrationCreate, RegistrationOut
from app.services import registration_service

router = APIRouter(prefix="/registrations", tags=["Registrations"])

Registrant = Annotated[User, Depends(require_permission(Permission.REGISTRATION_CREATE))]
Canceller = Annotated[User, Depends(require_permission(Permission.REGISTRATION_CANCEL_OWN))]

# Registering is cheap to abuse (seat hoarding): cap the per-IP rate in addition to the global cap.
_register_limit = RateLimit("register-event", lambda: max(get_settings().rate_limit_global_per_minute // 5, 10))


@router.post(
    "/",
    status_code=201,
    response_model=Envelope[RegistrationOut],
    summary="Register the authenticated user for an event",
    dependencies=[Depends(_register_limit)],
)
async def create_registration(body: RegistrationCreate, user: Registrant, db: DbSession, background: BackgroundTasks):
    # The registrant is ALWAYS the authenticated user: the body only carries the event id.
    registration = await registration_service.register_for_event(db, user, body.event_id, background)
    return ok(RegistrationOut.model_validate(registration), "Registration successful")


@router.get("/my", response_model=Envelope[Page[RegistrationOut]], summary="Own registrations")
async def my_registrations(
    user: CurrentUser,
    db: DbSession,
    page: PageParams,
    status: Annotated[Literal["confirmed", "cancelled"] | None, Query()] = None,
):
    rows, total = await registration_service.list_my_registrations(
        db, user, status=status, offset=page.offset, limit=page.page_size
    )
    items = [RegistrationOut.model_validate(r) for r in rows]
    return ok(paginated(items, total, page.page, page.page_size))


@router.get("/{registration_id}", response_model=Envelope[RegistrationOut], summary="One of your registrations")
async def get_registration(registration_id: uuid.UUID, user: CurrentUser, db: DbSession):
    registration = await registration_service.get_registration_for_user(db, registration_id, user)
    return ok(RegistrationOut.model_validate(registration))


@router.delete("/{registration_id}", response_model=Envelope[RegistrationOut], summary="Cancel own registration (releases the seat)")
async def cancel_registration(registration_id: uuid.UUID, user: Canceller, db: DbSession, background: BackgroundTasks):
    registration = await registration_service.cancel_registration(db, registration_id, user, background)
    return ok(RegistrationOut.model_validate(registration), "Registration cancelled")
