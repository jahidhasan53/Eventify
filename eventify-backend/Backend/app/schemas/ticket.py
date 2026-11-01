import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import ORMModel, StrictModel
from app.schemas.event import EventSummary


class TicketOut(ORMModel):
    id: uuid.UUID
    registration_id: uuid.UUID
    event_id: uuid.UUID
    status: str
    issued_at: datetime
    checked_in_at: datetime | None = None
    event: EventSummary


class TicketDetailOut(TicketOut):
    # Opaque verification token (what the QR code contains). Returned only to the ticket owner.
    qr_token: str


class TicketBrief(ORMModel):
    id: uuid.UUID
    status: str
    checked_in_at: datetime | None = None


class TicketTokenRequest(StrictModel):
    token: str = Field(min_length=10, max_length=200)


class TicketVerifyOut(StrictModel):
    valid: bool = True
    ticket_id: uuid.UUID
    event_id: uuid.UUID
    event_title: str
    attendee_name: str
    status: str


class CheckInOut(StrictModel):
    ticket_id: uuid.UUID
    event_id: uuid.UUID
    attendee_name: str
    checked_in_at: datetime
    checked_in_by_id: uuid.UUID


class AttendanceRecordOut(StrictModel):
    ticket_id: uuid.UUID
    attendee_id: uuid.UUID
    attendee_name: str
    checked_in_at: datetime
    checked_in_by_id: uuid.UUID | None = None
