import uuid
from datetime import datetime

from app.schemas.common import ORMModel, Page


class NotificationOut(ORMModel):
    id: uuid.UUID
    type: str
    title: str
    message: str
    event_id: uuid.UUID | None = None
    is_read: bool
    created_at: datetime


class NotificationPage(Page[NotificationOut]):
    unread_count: int = 0
