import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import RegistrationStatus, sql_values
from app.models.event import Event
from app.models.ticket import Ticket
from app.models.user import User


class Registration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "registrations"

    reference: Mapped[str] = mapped_column(String(24), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RegistrationStatus.CONFIRMED.value, server_default="confirmed"
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), server_default="0"
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    event: Mapped[Event] = relationship("Event", lazy="joined", innerjoin=True)
    ticket: Mapped[Ticket | None] = relationship("Ticket", back_populates="registration", uselist=False, lazy="joined")
    user: Mapped[User] = relationship("User", foreign_keys=[user_id], lazy="select")

    __table_args__ = (
        Index("uq_registrations_reference", "reference", unique=True),
        # DB-level guarantee: at most ONE active registration per user per event
        # (cancelled rows are kept for history and may repeat).
        Index(
            "uq_registrations_active_user_event",
            "user_id",
            "event_id",
            unique=True,
            postgresql_where=text("status = 'confirmed'"),
        ),
        Index("ix_registrations_event_status", "event_id", "status"),
        Index("ix_registrations_user_id", "user_id"),
        CheckConstraint(f"status IN ({sql_values(RegistrationStatus)})", name="ck_registrations_status"),
        CheckConstraint("amount >= 0", name="ck_registrations_amount"),
    )
