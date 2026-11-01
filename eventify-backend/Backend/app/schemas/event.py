import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import Field, computed_field, field_validator, model_validator

from app.schemas.common import ORMModel, StrictModel
from app.schemas.user import OrganizerBrief


def find_event_rule_violation(
    *,
    start: datetime,
    end: datetime,
    deadline: datetime,
    event_type: str,
    venue: str | None,
    capacity: int,
    ticket_type: str,
    price: Decimal,
    registered_count: int = 0,
    now: datetime | None = None,
) -> str | None:
    """Cross-field business rules shared by create (-> 422) and update (-> 400). Returns the first
    violation message, or None. Mirrors the CHECK constraints in the database."""
    if end <= start:
        return "end_datetime must be after start_datetime"
    if deadline > start:
        return "registration_deadline must not be after start_datetime"
    if now is not None and start <= now:
        return "start_datetime must be in the future"
    if capacity < 1 or capacity > 100_000:
        return "capacity must be between 1 and 100000"
    if capacity < registered_count:
        return f"capacity cannot be lower than the {registered_count} seats already registered"
    if event_type == "offline" and not (venue and venue.strip()):
        return "venue is required for offline events"
    if ticket_type == "free" and price != 0:
        return "ticket_price must be 0 for free events"
    if ticket_type == "paid" and price <= 0:
        return "ticket_price must be greater than 0 for paid events"
    return None


def _require_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must include a timezone offset (e.g. 2026-12-01T10:00:00Z)")
    return value.astimezone(timezone.utc)


def _check_cover_image(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not value.startswith(("https://", "http://")) or len(value) > 500:
        raise ValueError("cover_image must be an http(s) URL of at most 500 characters")
    return value


class EventCreate(StrictModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=10_000)
    category: str = Field(min_length=2, max_length=80)
    cover_image: str | None = None
    start_datetime: datetime
    end_datetime: datetime
    registration_deadline: datetime
    venue: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=255)
    event_type: Literal["online", "offline"] = "offline"
    capacity: int = Field(gt=0, le=100_000)
    ticket_type: Literal["free", "paid"] = "free"
    ticket_price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)

    _tz = field_validator("start_datetime", "end_datetime", "registration_deadline")(_require_aware)
    _cover = field_validator("cover_image")(_check_cover_image)

    @model_validator(mode="after")
    def _rules(self) -> "EventCreate":
        problem = find_event_rule_violation(
            start=self.start_datetime,
            end=self.end_datetime,
            deadline=self.registration_deadline,
            event_type=self.event_type,
            venue=self.venue,
            capacity=self.capacity,
            ticket_type=self.ticket_type,
            price=self.ticket_price,
            now=datetime.now(timezone.utc),
        )
        if problem:
            raise ValueError(problem)
        return self


class EventUpdate(StrictModel):
    """All fields optional. The merged result is re-validated in the service. Status, organizer,
    registered_count and featured flag are deliberately NOT updatable here."""

    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, min_length=10, max_length=10_000)
    category: str | None = Field(default=None, min_length=2, max_length=80)
    cover_image: str | None = None
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    registration_deadline: datetime | None = None
    venue: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=255)
    event_type: Literal["online", "offline"] | None = None
    capacity: int | None = Field(default=None, gt=0, le=100_000)
    ticket_type: Literal["free", "paid"] | None = None
    ticket_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)

    _tz = field_validator("start_datetime", "end_datetime", "registration_deadline")(_require_aware)
    _cover = field_validator("cover_image")(_check_cover_image)


class EventOut(ORMModel):
    id: uuid.UUID
    title: str
    description: str
    category: str
    cover_image: str | None = None
    organizer_id: uuid.UUID
    organizer: OrganizerBrief
    start_datetime: datetime
    end_datetime: datetime
    registration_deadline: datetime
    venue: str | None = None
    location: str | None = None
    event_type: str
    capacity: int
    registered_count: int
    ticket_type: str
    ticket_price: float
    status: str
    is_featured: bool
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def seats_remaining(self) -> int:
        return max(self.capacity - self.registered_count, 0)


class EventSummary(ORMModel):
    """Compact event info embedded in registrations, tickets and reviews."""

    id: uuid.UUID
    title: str
    category: str
    cover_image: str | None = None
    start_datetime: datetime
    end_datetime: datetime
    venue: str | None = None
    location: str | None = None
    event_type: str
    status: str


class EventModerationReject(StrictModel):
    reason: str = Field(min_length=3, max_length=1000)


class EventModerationNote(StrictModel):
    reason: str | None = Field(default=None, max_length=1000)


class EventFeatureUpdate(StrictModel):
    featured: bool


class EventReportCreate(StrictModel):
    reason: Literal["spam", "misleading", "inappropriate", "fraud", "other"]
    details: str | None = Field(default=None, max_length=2000)


class EventReportOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    reporter_id: uuid.UUID
    reason: str
    details: str | None = None
    status: str
    resolution_note: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    event: EventSummary | None = None


class EventReportResolve(StrictModel):
    status: Literal["resolved", "dismissed"]
    note: str | None = Field(default=None, max_length=1000)


def to_event_out(event, *, include_private: bool = False) -> EventOut:
    """Build EventOut; `rejection_reason` is shown only to the owner/admin (`include_private`)."""
    out = EventOut.model_validate(event)
    if not include_private:
        out.rejection_reason = None
    return out


__all__ = [
    "EventCreate",
    "EventFeatureUpdate",
    "EventModerationNote",
    "EventModerationReject",
    "EventOut",
    "EventReportCreate",
    "EventReportOut",
    "EventReportResolve",
    "EventSummary",
    "EventUpdate",
    "find_event_rule_violation",
    "to_event_out",
]
