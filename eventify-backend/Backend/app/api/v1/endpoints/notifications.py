import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select, update

from app.core.exceptions import NotFoundError
from app.database.connection import DbSession
from app.dependencies.authentication import CurrentUser
from app.dependencies.pagination import PageParams
from app.models.notification import Notification
from app.schemas.common import Envelope, ok, paginated
from app.schemas.notification import NotificationOut, NotificationPage

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("/", response_model=Envelope[NotificationPage], summary="Own notifications, newest first")
async def list_notifications(
    user: CurrentUser,
    db: DbSession,
    page: PageParams,
    unread_only: Annotated[bool, Query(description="Only unread notifications")] = False,
):
    clauses = [Notification.user_id == user.id]  # always scoped to the caller
    if unread_only:
        clauses.append(Notification.is_read.is_(False))
    total = (await db.execute(select(func.count()).select_from(Notification).where(*clauses))).scalar_one()
    unread = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(Notification)
            .where(*clauses)
            .order_by(Notification.created_at.desc(), Notification.id)
            .offset(page.offset)
            .limit(page.page_size)
        )
    ).scalars().all()
    data = paginated([NotificationOut.model_validate(n) for n in rows], total, page.page, page.page_size)
    data["unread_count"] = unread
    return ok(data)


@router.patch("/read-all", response_model=Envelope[dict], summary="Mark every notification as read")
async def mark_all_read(user: CurrentUser, db: DbSession):
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return ok({"updated": result.rowcount or 0}, "All notifications marked as read")


@router.patch("/{notification_id}/read", response_model=Envelope[NotificationOut], summary="Mark one notification as read")
async def mark_read(notification_id: uuid.UUID, user: CurrentUser, db: DbSession):
    # Ownership is part of the WHERE clause: another user's id behaves exactly like a missing one.
    result = await db.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user.id)
        .values(is_read=True)
        .execution_options(synchronize_session=False)
        .returning(Notification.id)
    )
    if result.scalar_one_or_none() is None:
        await db.rollback()
        raise NotFoundError("Notification not found")
    await db.commit()
    fresh = await db.execute(
        select(Notification).where(Notification.id == notification_id).execution_options(populate_existing=True)
    )
    return ok(NotificationOut.model_validate(fresh.scalar_one()), "Notification marked as read")
