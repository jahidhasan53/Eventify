"""String enumerations shared by models, schemas and services.

Values are stored as plain VARCHAR columns protected by CHECK constraints, which keeps
Alembic migrations simple (no PostgreSQL ENUM type alterations).
"""

from enum import StrEnum


class Role(StrEnum):
    ATTENDEE = "attendee"
    ORGANIZER = "organizer"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class EventStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class EventType(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class TicketType(StrEnum):
    FREE = "free"
    PAID = "paid"


class RegistrationStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class TicketStatus(StrEnum):
    VALID = "valid"
    USED = "used"
    CANCELLED = "cancelled"


class ReportStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class NotificationType(StrEnum):
    REGISTRATION_CONFIRMED = "registration_confirmed"
    REGISTRATION_CANCELLED = "registration_cancelled"
    EVENT_APPROVED = "event_approved"
    EVENT_REJECTED = "event_rejected"
    EVENT_PUBLISHED = "event_published"
    EVENT_REMINDER = "event_reminder"
    EVENT_UPDATED = "event_updated"
    EVENT_CANCELLED = "event_cancelled"


# Events visible to the public (anonymous visitors included)
PUBLIC_EVENT_STATUSES = (EventStatus.PUBLISHED.value, EventStatus.COMPLETED.value)


def sql_values(enum_cls: type[StrEnum]) -> str:
    """Render enum values as a SQL list for CHECK constraints: 'a', 'b', 'c'."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)
