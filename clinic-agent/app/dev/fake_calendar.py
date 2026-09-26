"""CALENDAR_MODE=fake: an in-DB calendar with the same interface as gcal.GoogleCalendar."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app.calendar.base import CalendarError, CalendarEvent
from app.db.models import FakeCalendarEvent
from app.db.session import aux_session as get_session


class FakeCalendar:
    def __init__(self) -> None:
        self.fail_next_insert = False  # tests flip this to simulate a Google outage

    async def freebusy(self, start_utc: datetime, end_utc: datetime):
        async with get_session() as db:
            rows = (
                await db.scalars(
                    select(FakeCalendarEvent).where(
                        FakeCalendarEvent.cancelled.is_(False),
                        FakeCalendarEvent.appointment_id.is_(None),
                        FakeCalendarEvent.start_utc < end_utc,
                        FakeCalendarEvent.end_utc > start_utc,
                    )
                )
            ).all()
            return [(r.start_utc, r.end_utc) for r in rows]

    async def insert(self, appointment_id, summary, description, start_utc, end_utc):
        if self.fail_next_insert:
            self.fail_next_insert = False
            raise CalendarError("simulated calendar failure")
        event_id = "fake_" + uuid.uuid4().hex[:16]
        async with get_session() as db:
            db.add(
                FakeCalendarEvent(
                    event_id=event_id,
                    summary=summary,
                    description=description,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    appointment_id=appointment_id,
                )
            )
            await db.commit()
        return event_id

    async def add_busy_block(self, start_utc: datetime, end_utc: datetime, summary: str = "Busy") -> str:
        event_id = "busy_" + uuid.uuid4().hex[:16]
        async with get_session() as db:
            db.add(FakeCalendarEvent(event_id=event_id, summary=summary, start_utc=start_utc, end_utc=end_utc))
            await db.commit()
        return event_id

    async def _row(self, db, event_id):
        return await db.scalar(select(FakeCalendarEvent).where(FakeCalendarEvent.event_id == event_id))

    async def patch(self, event_id, start_utc, end_utc):
        async with get_session() as db:
            row = await self._row(db, event_id)
            if row is None or row.cancelled:
                raise CalendarError("status=404")
            row.start_utc, row.end_utc = start_utc, end_utc
            await db.commit()

    async def delete(self, event_id):
        async with get_session() as db:
            row = await self._row(db, event_id)
            if row is not None:
                row.cancelled = True
                await db.commit()

    async def get(self, event_id):
        async with get_session() as db:
            row = await self._row(db, event_id)
            if row is None or row.cancelled:
                return None
            return CalendarEvent(row.event_id, row.start_utc, row.end_utc, row.appointment_id, row.summary)

    async def list_all(self) -> list[FakeCalendarEvent]:
        async with get_session() as db:
            return list(
                (
                    await db.scalars(
                        select(FakeCalendarEvent)
                        .where(FakeCalendarEvent.cancelled.is_(False))
                        .order_by(FakeCalendarEvent.start_utc)
                    )
                ).all()
            )
