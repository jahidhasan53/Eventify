import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.utils import utcnow
from app.database.base import Base, UUIDPrimaryKeyMixin
from app.models.enums import TicketStatus, sql_values
from app.models.event import Event
from app.models.user import User

if TYPE_CHECKING:
    from app.models.registration import Registration


class Ticket(UUIDPrimaryKeyMixin, Base):
    """One ticket per registration. The QR token is NOT stored: it is derived as
    HMAC(server key, ticket_id + token_nonce), so a database leak alone cannot forge tickets
    and the QR can be regenerated on demand."""

    __tablename__ = "tickets"

    registration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registrations.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TicketStatus.VALID.value, server_default="valid"
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    event: Mapped[Event] = relationship("Event", lazy="joined", innerjoin=True)
    registration: Mapped["Registration"] = relationship("Registration", back_populates="ticket", lazy="select")
    attendee: Mapped[User] = relationship("User", foreign_keys=[user_id], lazy="select")

    __table_args__ = (
        UniqueConstraint("registration_id", name="uq_tickets_registration_id"),
        Index("ix_tickets_event_status", "event_id", "status"),
        Index("ix_tickets_user_id", "user_id"),
        CheckConstraint(f"status IN ({sql_values(TicketStatus)})", name="ck_tickets_status"),
        # A ticket is 'used' exactly when it has a check-in timestamp.
        CheckConstraint("(status = 'used') = (checked_in_at IS NOT NULL)", name="ck_tickets_checkin_consistent"),
    )
