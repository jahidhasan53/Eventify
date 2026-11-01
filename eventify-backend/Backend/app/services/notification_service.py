"""In-app notifications (+ optional e-mail). Rows join the caller's transaction (no commit here);
e-mails are queued as background tasks so they are only sent after the response is produced."""

import uuid
from collections.abc import Iterable

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.services.email_service import send_email


def notify(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    email: str | None,
    kind: str,
    title: str,
    message: str,
    event_id: uuid.UUID | None = None,
    background: BackgroundTasks | None = None,
) -> Notification:
    notification = Notification(user_id=user_id, type=kind, title=title, message=message, event_id=event_id)
    db.add(notification)
    if background is not None and email:
        background.add_task(send_email, email, title, message)
    return notification


def notify_many(
    db: AsyncSession,
    recipients: Iterable[tuple[uuid.UUID, str | None]],
    *,
    kind: str,
    title: str,
    message: str,
    event_id: uuid.UUID | None = None,
    background: BackgroundTasks | None = None,
) -> int:
    count = 0
    for user_id, email in recipients:
        notify(
            db, user_id=user_id, email=email, kind=kind, title=title, message=message,
            event_id=event_id, background=background,
        )
        count += 1
    return count
