"""Import every model so SQLAlchemy/Alembic see the full metadata."""

from app.models.audit import AuditLog
from app.models.event import Event, EventReport
from app.models.notification import Notification
from app.models.registration import Registration
from app.models.review import Review
from app.models.ticket import Ticket
from app.models.user import PasswordResetToken, RefreshToken, User

__all__ = [
    "AuditLog",
    "Event",
    "EventReport",
    "Notification",
    "PasswordResetToken",
    "RefreshToken",
    "Registration",
    "Review",
    "Ticket",
    "User",
]
