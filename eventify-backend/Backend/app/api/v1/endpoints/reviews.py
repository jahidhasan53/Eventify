import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import Permission
from app.database.connection import DbSession
from app.dependencies.authentication import CurrentUser, OptionalUser
from app.dependencies.authorization import require_permission
from app.dependencies.pagination import PageParams
from app.models.user import User
from app.schemas.common import Envelope, ok, paginated
from app.schemas.review import ReviewCreate, ReviewOut, ReviewPage
from app.services import event_service, review_service

router = APIRouter(prefix="/reviews", tags=["Reviews"])

Reviewer = Annotated[User, Depends(require_permission(Permission.REVIEW_CREATE))]


@router.post(
    "/",
    status_code=201,
    response_model=Envelope[ReviewOut],
    summary="Review a completed event you attended (one review per event)",
)
async def create_review(body: ReviewCreate, user: Reviewer, db: DbSession):
    review = await review_service.create_review(db, user, body)  # reviewer = authenticated user, never the body
    return ok(ReviewOut.model_validate(review), "Review submitted successfully")


@router.get("/event/{event_id}", response_model=Envelope[ReviewPage], summary="Reviews of an event (public events only)")
async def event_reviews(event_id: uuid.UUID, db: DbSession, page: PageParams, viewer: OptionalUser):
    event = await event_service.get_visible_event(db, event_id, viewer)
    reviews, total, average = await review_service.list_event_reviews(
        db, event.id, offset=page.offset, limit=page.page_size
    )
    data = paginated([ReviewOut.model_validate(r) for r in reviews], total, page.page, page.page_size)
    data["average_rating"] = average
    return ok(data)


@router.delete("/{review_id}", response_model=Envelope[None], summary="Delete own review (admins may moderate any)")
async def delete_review(review_id: uuid.UUID, user: CurrentUser, db: DbSession):
    await review_service.delete_review(db, review_id, user)
    return ok(None, "Review deleted")
