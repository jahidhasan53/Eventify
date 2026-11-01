import uuid
from datetime import datetime

from app.schemas.common import StrictModel
from app.schemas.user import UserOut


class RecentRegistrationOut(StrictModel):
    registration_id: uuid.UUID
    reference: str
    status: str
    registered_at: datetime
    event_id: uuid.UUID
    event_title: str
    attendee_name: str


class OrganizerDashboardOut(StrictModel):
    total_events: int
    published_events: int
    upcoming_events: int
    total_registrations: int
    total_attendees: int
    attendance_percentage: float
    recent_registrations: list[RecentRegistrationOut]


class EventStatisticsOut(StrictModel):
    event_id: uuid.UUID
    title: str
    status: str
    capacity: int
    registered_count: int
    seats_remaining: int
    active_registrations: int
    cancelled_registrations: int
    checked_in: int
    attendance_percentage: float
    revenue: float
    review_count: int
    average_rating: float | None = None


class OrganizerProfileOut(StrictModel):
    user: UserOut
    total_events: int
    total_registrations: int
    total_attendees: int
