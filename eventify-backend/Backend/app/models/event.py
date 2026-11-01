import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.utils import utcnow
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import EventStatus, EventType, ReportStatus, TicketType, sql_values
from app.models.user import User


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "events"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    cover_image: Mapped[str | None] = mapped_column(String(500))
    organizer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    start_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registration_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    venue: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(
        String(10), nullable=False, default=EventType.OFFLINE.value, server_default="offline"
    )

    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Maintained ONLY through atomic UPDATEs in registration_service (never read-modify-write).
    registered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    ticket_type: Mapped[str] = mapped_column(
        String(10), nullable=False, default=TicketType.FREE.value, server_default="free"
    )
    ticket_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), server_default="0"
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EventStatus.DRAFT.value, server_default="draft"
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organizer: Mapped[User] = relationship("User", lazy="joined", innerjoin=True)

    __table_args__ = (
        CheckConstraint("capacity > 0 AND capacity <= 100000", name="ck_events_capacity_range"),
        CheckConstraint("registered_count >= 0 AND registered_count <= capacity", name="ck_events_registered_count"),
        CheckConstraint("end_datetime > start_datetime", name="ck_events_dates_order"),
        CheckConstraint("registration_deadline <= start_datetime", name="ck_events_deadline_before_start"),
        CheckConstraint(
            "(ticket_type = 'free' AND ticket_price = 0) OR (ticket_type = 'paid' AND ticket_price > 0)",
            name="ck_events_price_matches_type",
        ),
        CheckConstraint(f"event_type IN ({sql_values(EventType)})", name="ck_events_event_type"),
        CheckConstraint(f"ticket_type IN ({sql_values(TicketType)})", name="ck_events_ticket_type"),
        CheckConstraint(f"status IN ({sql_values(EventStatus)})", name="ck_events_status"),
        Index("ix_events_status_start", "status", "start_datetime"),
        Index("ix_events_organizer_status", "organizer_id", "status"),
        Index("ix_events_category", "category"),
    )


class EventReport(UUIDPrimaryKeyMixin, Base):
    """A user's report of an inappropriate/misleading event, handled by admins."""

    __tablename__ = "event_reports"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    reporter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReportStatus.OPEN.value, server_default="open"
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()")
    )

    event: Mapped[Event] = relationship("Event", lazy="joined")

    __table_args__ = (
        UniqueConstraint("event_id", "reporter_id", name="uq_event_reports_event_reporter"),
        CheckConstraint(f"status IN ({sql_values(ReportStatus)})", name="ck_event_reports_status"),
        Index("ix_event_reports_status", "status"),
    )
