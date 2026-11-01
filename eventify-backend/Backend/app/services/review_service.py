"""Reviews: only attendees who were actually checked in may review a COMPLETED event, once."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.enums import EventStatus, Role, TicketStatus
from app.models.review import Review
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.review import ReviewCreate
from app.services.event_service import get_event


async def _load_review(db: AsyncSession, review_id: uuid.UUID) -> Review:
    result = await db.execute(select(Review).where(Review.id == review_id).execution_options(populate_existing=True))
    review = result.scalar_one_or_none()
    if review is None:
        raise NotFoundError("Review not found")
    return review


async def create_review(db: AsyncSession, user: User, data: ReviewCreate) -> Review:
    event = await get_event(db, data.event_id)
    if event.status != EventStatus.COMPLETED.value:
        raise ConflictError("Reviews can only be submitted for completed events")

    attended = await db.execute(
        select(Ticket.id).where(
            Ticket.event_id == event.id,
            Ticket.user_id == user.id,
            Ticket.status == TicketStatus.USED.value,  # checked in = actually attended
        )
    )
    if attended.first() is None:
        raise ForbiddenError("Only attendees who checked in to this event can review it")

    review = Review(event_id=event.id, user_id=user.id, rating=data.rating, comment=data.comment)
    db.add(review)
    try:
        await db.commit()
    except IntegrityError:  # unique (event_id, user_id)
        await db.rollback()
        raise ConflictError("You have already reviewed this event") from None
    return await _load_review(db, review.id)


async def list_event_reviews(
    db: AsyncSession, event_id: uuid.UUID, *, offset: int, limit: int
) -> tuple[list[Review], int, float | None]:
    total = (await db.execute(select(func.count()).select_from(Review).where(Review.event_id == event_id))).scalar_one()
    average = (await db.execute(select(func.avg(Review.rating)).where(Review.event_id == event_id))).scalar_one()
    result = await db.execute(
        select(Review)
        .where(Review.event_id == event_id)
        .order_by(Review.created_at.desc(), Review.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total, (round(float(average), 2) if average is not None else None)


async def delete_review(db: AsyncSession, review_id: uuid.UUID, user: User) -> None:
    """Ownership check: authors may delete their own review; admins may moderate any."""
    review = await _load_review(db, review_id)
    if review.user_id != user.id and user.role != Role.ADMIN.value:
        raise NotFoundError("Review not found")  # do not confirm that other people's review ids exist
    await db.delete(review)
    await db.commit()
