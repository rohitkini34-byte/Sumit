import asyncio

import pytest

from app.db.models import Appointment, FakeCalendarEvent, OutboxMessage
from app.scheduling import booking_service
from app.scheduling.booking_service import BookingLimit, CalendarUnavailable, InvalidSlot, SlotTaken
from tests.conftest import book, flush_outbox, get_appt, ist, make_patient, rows


async def test_book_creates_event_token_and_confirmation(wa):
    p = await make_patient()
    a = await book(p, "2026-09-28 10:30")
    assert a.token_label == "M-03" and a.queue_status == "scheduled" and a.status == "booked"
    ev = [e for e in await rows(FakeCalendarEvent) if e.appointment_id == str(a.id)]
    assert len(ev) == 1 and ev[0].summary == "Appt – Asha P." and "Ref:" in ev[0].description
    assert "Asha Patil" not in ev[0].summary and "+91" not in (ev[0].description or "")
    await flush_outbox()
    msg = wa.for_phone("+919800000001")[-1]
    assert "M-03" in msg.text and "10:30 AM" in msg.text and "report by 10:20 AM" in msg.text
    assert "10 minutes late" in msg.text  # the grace rule line


async def test_confirmation_outside_window_uses_template(wa):
    p = await make_patient(inbound=False)
    await book(p, "2026-09-28 10:30")
    await flush_outbox()
    m = wa.for_phone("+919800000001")[-1]
    assert m.kind == "template" and m.template == "appt_confirmation"


async def test_twenty_concurrent_bookings_one_wins():
    patients = [await make_patient(f"+91980000{i:04d}", f"P {i}") for i in range(20)]
    results = await asyncio.gather(*[book(p, "2026-09-28 11:00") for p in patients], return_exceptions=True)
    ok = [r for r in results if isinstance(r, Appointment)]
    errors = [r for r in results if not isinstance(r, Appointment)]
    assert len(ok) == 1
    assert all(isinstance(e, SlotTaken) for e in errors), errors
    booked = await rows(Appointment, Appointment.status == "booked")
    assert len(booked) == 1
    events = [e for e in await rows(FakeCalendarEvent) if e.appointment_id and not e.cancelled]
    assert len(events) == 1


async def test_booking_limit():
    p = await make_patient()
    await book(p, "2026-09-28 10:00", enforce_limit=True)
    await book(p, "2026-09-28 10:15", enforce_limit=True)
    with pytest.raises(BookingLimit):
        await book(p, "2026-09-28 10:30", enforce_limit=True)


async def test_calendar_failure_rolls_back(cal):
    p = await make_patient()
    cal.fail_next_insert = True
    with pytest.raises(CalendarUnavailable):
        await book(p, "2026-09-28 10:00")
    assert await rows(Appointment) == []
    assert await rows(OutboxMessage) == []


async def test_busy_block_blocks_booking(cal):
    p = await make_patient()
    await cal.add_busy_block(ist("2026-09-28 10:10"), ist("2026-09-28 10:40"))
    with pytest.raises(SlotTaken):
        await book(p, "2026-09-28 10:15")
    from app.db.session import get_session
    from datetime import date

    async with get_session() as db:
        free = await booking_service.find_slots(db, date(2026, 9, 28), date(2026, 9, 28), "morning")
    hm = [s.astimezone(ist("2026-09-28 10:00").tzinfo).strftime("%H:%M") for s in free]
    assert "10:00" not in hm and "10:15" not in hm and "10:30" not in hm and "10:45" in hm


async def test_invalid_or_past_slot_rejected():
    p = await make_patient()
    with pytest.raises(InvalidSlot):
        await book(p, "2026-09-28 10:07")
    with pytest.raises(InvalidSlot):
        await book(p, "2026-09-28 06:00")
    with pytest.raises(InvalidSlot):
        await book(p, "2026-09-27 10:00")


async def test_rebooking_a_cancelled_slot_reuses_token():
    from app.scheduling import change_service

    p1, p2 = await make_patient(), await make_patient("+919800000002", "Ravi K")
    a = await book(p1, "2026-09-28 10:30")
    await change_service.cancel(a.id, by="patient", actor="patient")
    b = await book(p2, "2026-09-28 10:30")
    assert b.token_label == a.token_label == "M-03"
    assert (await get_appt(a.id)).status == "cancelled"
