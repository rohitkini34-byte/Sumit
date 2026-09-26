"""Calendar interface shared by the Google client and the in-DB fake."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class CalendarError(Exception):
    pass


@dataclass
class CalendarEvent:
    event_id: str
    start_utc: datetime
    end_utc: datetime
    appointment_id: str | None
    summary: str = ""


class CalendarBackend(Protocol):
    async def freebusy(self, start_utc: datetime, end_utc: datetime) -> list[tuple[datetime, datetime]]:
        """Busy blocks NOT created by this app (the doctor's own events)."""

    async def insert(
        self, appointment_id: str, summary: str, description: str, start_utc: datetime, end_utc: datetime
    ) -> str:
        """Create an event; returns its event id."""

    async def add_busy_block(self, start_utc: datetime, end_utc: datetime, summary: str = "Busy") -> str:
        """A block of time that is not an appointment (the doctor's own event)."""

    async def patch(self, event_id: str, start_utc: datetime, end_utc: datetime) -> None: ...

    async def delete(self, event_id: str) -> None:
        """Idempotent: deleting a missing event is not an error."""

    async def get(self, event_id: str) -> CalendarEvent | None:
        """None when the event was deleted or cancelled."""


_backend: CalendarBackend | None = None


def get_calendar() -> CalendarBackend:
    global _backend
    if _backend is None:
        from app.config import get_settings

        if get_settings().CALENDAR_MODE == "google":
            from app.calendar.gcal import GoogleCalendar

            _backend = GoogleCalendar.from_settings()
        else:
            from app.dev.fake_calendar import FakeCalendar

            _backend = FakeCalendar()
    return _backend


def set_calendar(backend: CalendarBackend | None) -> None:
    global _backend
    _backend = backend
