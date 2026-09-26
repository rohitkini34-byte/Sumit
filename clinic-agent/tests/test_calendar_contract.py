"""The fake calendar and the Google wrapper share one contract suite. The Google wrapper runs
against an in-memory stand-in for the discovery client here, and against the real
"Clinic TEST" calendar with -m staging."""
import itertools
import os

import pytest

from app.calendar.base import CalendarError
from app.calendar.gcal import GoogleCalendar
from app.dev.fake_calendar import FakeCalendar
from tests.conftest import ist


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeGoogleService:
    """Minimal stand-in for googleapiclient's events() resource."""

    class HttpErr(Exception):
        def __init__(self, status):
            self.resp = type("R", (), {"status": status})()

    def __init__(self):
        self.events_store = {}
        self.ids = itertools.count(1)

    def events(self):
        return self

    def insert(self, calendarId, body):
        def run():
            eid = f"g{next(self.ids)}"
            self.events_store[eid] = {"id": eid, "status": "confirmed", **body}
            return self.events_store[eid]
        return _Req(run)

    def get(self, calendarId, eventId):
        def run():
            if eventId not in self.events_store:
                raise self.HttpErr(404)
            return self.events_store[eventId]
        return _Req(run)

    def patch(self, calendarId, eventId, body):
        def run():
            if eventId not in self.events_store or self.events_store[eventId]["status"] == "cancelled":
                raise self.HttpErr(404)
            self.events_store[eventId].update(body)
            return self.events_store[eventId]
        return _Req(run)

    def delete(self, calendarId, eventId):
        def run():
            if eventId not in self.events_store:
                raise self.HttpErr(410)
            self.events_store[eventId]["status"] = "cancelled"
            return {}
        return _Req(run)

    def list(self, calendarId, timeMin, timeMax, **kw):
        from app.calendar.gcal import _parse

        def run():
            from datetime import datetime

            lo = datetime.fromisoformat(timeMin.replace("Z", "+00:00"))
            hi = datetime.fromisoformat(timeMax.replace("Z", "+00:00"))
            items = [e for e in self.events_store.values()
                     if e["status"] != "cancelled" and _parse(e["start"]) < hi and _parse(e["end"]) > lo]
            return {"items": items}
        return _Req(run)


@pytest.fixture(params=["fake", "google-stub", "google-real"])
def backend(request):
    if request.param == "fake":
        return FakeCalendar()
    if request.param == "google-stub":
        return GoogleCalendar(FakeGoogleService(), "cal")
    if not os.environ.get("GOOGLE_CALENDAR_ID") or not request.config.getoption("-m") or "staging" not in request.config.getoption("-m"):
        pytest.skip("real Google calendar only with -m staging and credentials")
    return GoogleCalendar.from_settings()


async def test_contract_insert_get_patch_delete(backend):
    s, e = ist("2026-09-28 10:00"), ist("2026-09-28 10:15")
    eid = await backend.insert("appt-1", "Appt – A B.", "Booked via WhatsApp. Ref: X", s, e)
    ev = await backend.get(eid)
    assert ev is not None and ev.appointment_id == "appt-1" and ev.start_utc == s
    s2, e2 = ist("2026-09-28 11:00"), ist("2026-09-28 11:15")
    await backend.patch(eid, s2, e2)
    assert (await backend.get(eid)).start_utc == s2
    await backend.delete(eid)
    assert await backend.get(eid) is None
    await backend.delete(eid)  # idempotent
    with pytest.raises(CalendarError):
        await backend.patch(eid, s, e)


async def test_contract_freebusy_excludes_own_appointments(backend):
    await backend.insert("appt-2", "Appt", "d", ist("2026-09-28 10:00"), ist("2026-09-28 10:15"))
    await backend.add_busy_block(ist("2026-09-28 12:00"), ist("2026-09-28 12:30"), "Doctor busy")
    busy = await backend.freebusy(ist("2026-09-28 00:00"), ist("2026-09-29 00:00"))
    assert busy == [(ist("2026-09-28 12:00"), ist("2026-09-28 12:30"))]
    assert await backend.freebusy(ist("2026-09-28 13:00"), ist("2026-09-28 14:00")) == []
