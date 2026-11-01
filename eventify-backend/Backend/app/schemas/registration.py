import uuid
from datetime import datetime

from app.schemas.common import ORMModel, StrictModel
from app.schemas.event import EventSummary
from app.schemas.ticket import TicketBrief


class RegistrationCreate(StrictModel):
    event_id: uuid.UUID


class RegistrationOut(ORMModel):
    id: uuid.UUID
    reference: str
    event_id: uuid.UUID
    status: str
    amount: float
    created_at: datetime
    cancelled_at: datetime | None = None
    event: EventSummary
    ticket: TicketBrief | None = None


class AttendeeOut(StrictModel):
    registration_id: uuid.UUID
    reference: str
    status: str
    registered_at: datetime
    user_id: uuid.UUID
    full_name: str
    email: str
    ticket_id: uuid.UUID | None = None
    ticket_status: str | None = None
    checked_in_at: datetime | None = None
