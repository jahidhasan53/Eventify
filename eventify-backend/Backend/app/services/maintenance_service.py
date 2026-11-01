"""Periodic housekeeping: mark ended events as completed and send 24-hour reminders.

Safe to run in several workers at once: reminders are *claimed* with a conditional UPDATE
(reminder_sent_at IS NULL), so each event is reminded exactly once.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.database.connection import SessionLocal
from app.models.enums import EventStatus, NotificationType
from app.models.event import Event
from app.services.email_service import send_email
from app.services.event_service import confirmed_registrants
from app.services.notification_service import notify

logger = logging.getLogger("eventify.maintenance")

CYCLE_SECONDS = 60
REMINDER_WINDOW = timedelta(hours=24)


async def run_maintenance_cycle(db: AsyncSession) -> dict[str, int]:
    now = utcnow()

    completed = await db.execute(
        update(Event)
        .where(Event.status == EventStatus.PUBLISHED.value, Event.end_datetime <= now)
        .values(status=EventStatus.COMPLETED.value)
        .execution_options(synchronize_session=False)
        .returning(Event.id)
    )
    completed_count = len(completed.all())

    claimed = await db.execute(
        update(Event)
        .where(
            Event.status == EventStatus.PUBLISHED.value,
            Event.reminder_sent_at.is_(None),
            Event.start_datetime > now,
            Event.start_datetime <= now + REMINDER_WINDOW,
        )
        .values(reminder_sent_at=now)
        .execution_options(synchronize_session=False)
        .returning(Event.id, Event.title, Event.start_datetime)
    )
    reminders = claimed.all()

    emails: list[tuple[str, str, str]] = []
    reminded = 0
    for event_id, title, start in reminders:
        message = f"Reminder: '{title}' starts on {start:%Y-%m-%d %H:%M} UTC. Don't forget your QR ticket!"
        for user_id, email in await confirmed_registrants(db, event_id):
            notify(
                db, user_id=user_id, email=None, kind=NotificationType.EVENT_REMINDER.value,
                title=f"Reminder: {title}", message=message, event_id=event_id,
            )
            emails.append((email, f"Reminder: {title}", message))
            reminded += 1
    await db.commit()

    for to, subject, body in emails:  # after commit; send_email never raises
        await send_email(to, subject, body)
    return {"completed": completed_count, "reminders": reminded}


async def maintenance_loop() -> None:
    while True:
        try:
            async with SessionLocal() as db:
                stats = await run_maintenance_cycle(db)
            if stats["completed"] or stats["reminders"]:
                logger.info("Maintenance cycle: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Maintenance cycle failed")
        await asyncio.sleep(CYCLE_SECONDS)
