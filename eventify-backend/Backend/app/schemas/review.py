import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import ORMModel, Page, StrictModel
from app.schemas.user import UserBrief


class ReviewCreate(StrictModel):
    event_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class ReviewOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    rating: int
    comment: str | None = None
    created_at: datetime
    reviewer: UserBrief


class ReviewPage(Page[ReviewOut]):
    average_rating: float | None = None
