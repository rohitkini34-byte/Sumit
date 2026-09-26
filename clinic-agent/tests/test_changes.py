"""Section 10B: cancellations, reschedules, freed-slot offers, waitlist, clinic-side changes."""
import asyncio
from datetime import date

import pytest

from app.db.models import Appointment, FakeCalendarEvent, OutboxMessage, Patient, ReminderLog, SlotOffer, Waitlist
from app.db.session import get_session
from app.dev import clock
from app.scheduling import booking_service, change_service, offers, queue_service as Q, reminders
from tests.conftest import book, flush_outbox, get_appt, get_patient, ist, make_patient, rows, tune

D = date(2026, 9, 28)
TIMES = ["10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30", "11:45"]


async def setup(n=5, day="2026-09-28"):
    out = []
    for i in range(n):
        p = await make_patient(f"+9198000000{i:02d}", f"Pat{i} X")
        out.append(await book(p, f"{day} {TIMES[i]}"))
    await flush_outbox()
    return out


def phone(i):
    return f"+9198000000{i:02d}"


async def test_cancel_side_effects(wa, cal):
    tune(changes={"offer_earlier_slot": {"enabled": False}, "waitlist": {"enabled": False}})
    a = await setup(3)
    clock.set_now(ist("2026-09-28 09:50"))
    async with get_session() as db:
        _, before = await Q.session_etas(db, D, "morning")
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    c = await get_appt(a[0].id)
    assert (c.status, c.queue_status, c.cancelled_by) == ("cancelled", "cancelled", "patient")
    assert c.is_late_cancel  # within 120 min of the slot
    assert (await get_patient(c.patient_id)).late_cancel_count == 1
    async with get_session() as db:
        _, after = await Q.session_etas(db, D, "morning")
    assert a[0].id not in after
    assert after[a[1].id].ahead == before[a[1].id].ahead - 1
    await flush_outbox()
    assert await cal.get(c.gcal_event_id) is None  # calendar event deleted
    assert "has been cancelled" in wa.for_phone(phone(0))[-1].text
    async with get_session() as db:
        free = await booking_service.find_slots(db, D, D, "morning")
    assert ist("2026-09-28 10:15") not in free  # still booked
    clock.set_now(ist("2026-09-28 07:00"))
    async with get_session() as db:
        free = await booking_service.find_slots(db, D, D, "morning")
    assert ist("2026-09-28 10:00") in free  # released


async def test_sequential_tokens_renumber_before_freeze_once_per_patient(wa):
    tune(changes={"token_mode": "sequential", "offer_earlier_slot": {"enabled": False}})
    a = await setup(4, day="2026-09-29")  # Tuesday; now is Monday 07:00 (before 20:00 freeze)
    assert [x.token_label for x in [await get_appt(x.id) for x in a]] == ["M-01", "M-02", "M-03", "M-04"]
    await change_service.cancel(a[1].id, by="patient", actor="patient")
    labels = [(await get_appt(x.id)).token_label for x in (a[0], a[2], a[3])]
    assert labels == ["M-01", "M-02", "M-03"]
    await flush_outbox()
    for i in (2, 3):
        upd = [m for m in wa.for_phone(phone(i)) if "token number has changed" in m.text]
        assert len(upd) == 1
    assert not [m for m in wa.for_phone(phone(0)) if "token number has changed" in m.text]


async def test_sequential_tokens_frozen_after_freeze(wa):
    tune(changes={"token_mode": "sequential", "offer_earlier_slot": {"enabled": False}})
    a = await setup(3, day="2026-09-29")
    clock.set_now(ist("2026-09-28 20:30"))
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    assert [(await get_appt(x.id)).token_label for x in a[1:]] == ["M-02", "M-03"]
    p = await make_patient("+919811111111", "New P")
    n = await book(p, "2026-09-29 11:30")
    assert n.token_label == "M-04"  # frozen: appended, nobody renumbered


async def test_slot_mode_no_token_changes(wa):
    a = await setup(3)
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    await flush_outbox()
    assert [(await get_appt(x.id)).token_label for x in a[1:]] == ["M-02", "M-03"]
    assert not any("token number" in m.text for m in wa.sent)


async def test_reschedule_to_taken_slot_leaves_old_untouched():
    a = await setup(2)
    with pytest.raises(booking_service.SlotTaken):
        await change_service.reschedule(a[0].id, a[1].start_utc, by="patient", actor="patient")
    old = await get_appt(a[0].id)
    assert old.status == "booked" and old.token_label == "M-01"


async def test_reschedule_sends_only_rescheduled_and_old_reminders_never_sent(wa, cal):
    a = await setup(1)
    wa.clear()
    new = await change_service.reschedule(a[0].id, ist("2026-09-29 11:00"), by="patient", actor="patient")
    await flush_outbox()
    msgs = wa.for_phone(phone(0))
    assert len(msgs) == 1 and "has moved to" in msgs[0].text and new.token_label in msgs[0].text
    assert "11:00 AM" in msgs[0].text
    old = await get_appt(a[0].id)
    assert old.status == "rescheduled" and new.previous_token_label == "M-01" and new.rescheduled_from_id == old.id
    ev = await cal.get(new.gcal_event_id)  # one event, patched to the new time
    assert ev.start_utc == ist("2026-09-29 11:00")
    # at the old reminder time nothing goes out for the old appointment
    clock.set_now(ist("2026-09-28 08:30"))
    await reminders.run_appointment_reminders()
    assert await rows(ReminderLog, ReminderLog.appointment_id == old.id) == []


async def test_move_earlier_two_accept_one_wins(wa):
    a = await setup(5)
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    offs = await rows(SlotOffer, SlotOffer.status == "open")
    assert sorted(o.offered_to_appointment_id for o in offs) == sorted(x.id for x in a[1:4])  # next 3 later patients
    await flush_outbox()
    assert "earlier time has opened" in wa.for_phone(phone(1))[-1].text
    ob = next(o for o in offs if o.offered_to_appointment_id == a[1].id)
    oc = next(o for o in offs if o.offered_to_appointment_id == a[2].id)
    res = await asyncio.gather(offers.accept_offer(ob.id, ob.patient_id), offers.accept_offer(oc.id, oc.patient_id),
                               return_exceptions=True)
    winners = [r for r in res if isinstance(r, Appointment)]
    losers = [r for r in res if isinstance(r, offers.OfferUnavailable)]
    assert len(winners) == 1 and len(losers) == 1
    moved = winners[0]
    assert moved.start_utc == a[0].start_utc and moved.token_label == "M-01"
    booked_at_slot = await rows(Appointment, Appointment.start_utc == a[0].start_utc, Appointment.status == "booked")
    assert len(booked_at_slot) == 1
    # tapping the old button later gets "already taken"
    with pytest.raises(offers.OfferUnavailable):
        await offers.accept_offer(oc.id if moved.patient_id == ob.patient_id else ob.id,
                                  oc.patient_id if moved.patient_id == ob.patient_id else ob.patient_id)


async def test_cascade_stops_then_waitlist_then_next_on_expiry(wa):
    tune(changes={"offer_earlier_slot": {"max_cascade": 2}})
    a = await setup(6)
    w1 = await make_patient("+919877777701", "Wait One")
    w2 = await make_patient("+919877777702", "Wait Two")
    await offers.join_waitlist(w1.id, D, "morning")
    clock.advance(1)
    await offers.join_waitlist(w2.id, D, "any")
    await change_service.cancel(a[0].id, by="patient", actor="patient")  # frees 10:00 (depth 0)
    o = next(o for o in await rows(SlotOffer, SlotOffer.status == "open") if o.offered_to_appointment_id == a[1].id)
    await offers.accept_offer(o.id, o.patient_id)  # B -> 10:00, frees 10:15 (depth 1)
    o = next(o for o in await rows(SlotOffer, SlotOffer.status == "open") if o.offered_to_appointment_id == a[2].id)
    assert o.cascade_depth == 1 and o.slot_start_utc == ist("2026-09-28 10:15")
    await offers.accept_offer(o.id, o.patient_id)  # C -> 10:15, frees 10:30 (depth 2 = max) -> waitlist
    open_ = await rows(SlotOffer, SlotOffer.status == "open")
    assert len(open_) == 1 and open_[0].offer_type == "waitlist" and open_[0].patient_id == w1.id
    assert open_[0].slot_start_utc == ist("2026-09-28 10:30")
    clock.advance(16)
    await offers.expire_offers()
    open_ = await rows(SlotOffer, SlotOffer.status == "open")
    assert len(open_) == 1 and open_[0].patient_id == w2.id
    assert (await rows(Waitlist, Waitlist.patient_id == w1.id))[0].status == "expired"
    new = await offers.accept_offer(open_[0].id, w2.id)
    assert new.token_label == "M-03" and new.start_utc == ist("2026-09-28 10:30")
    assert (await rows(Waitlist, Waitlist.patient_id == w2.id))[0].status == "booked"


async def test_declining_all_offers_goes_to_waitlist():
    a = await setup(2)
    w = await make_patient("+919877777701", "Wait One")
    await offers.join_waitlist(w.id, D)
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    o = (await rows(SlotOffer, SlotOffer.status == "open"))[0]
    await offers.decline_offer(o.id, o.patient_id)
    open_ = await rows(SlotOffer, SlotOffer.status == "open")
    assert open_[0].offer_type == "waitlist" and open_[0].patient_id == w.id


async def test_no_offers_within_min_lead():
    a = await setup(3)
    clock.set_now(ist("2026-09-28 09:00"))  # 10:00 is only 60 min away (< 90)
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    assert await rows(SlotOffer) == []


async def test_checked_in_patient_cannot_cancel_via_whatsapp_but_staff_can():
    a = await setup(1)
    clock.set_now(ist("2026-09-28 09:55"))
    await Q.check_in(a[0].id, "t")
    with pytest.raises(change_service.NotAllowed):
        await change_service.cancel(a[0].id, by="patient", actor="patient")
    await change_service.cancel(a[0].id, by="staff", actor="admin:x")
    assert (await get_appt(a[0].id)).status == "cancelled"


async def test_clinic_session_cancel(wa):
    a = await setup(3)
    w = await make_patient("+919877777701", "Wait One")
    await offers.join_waitlist(w.id, D)
    clock.set_now(ist("2026-09-28 09:30"))
    n = await change_service.cancel_session_by_clinic(D, "morning", "admin:x")
    assert n == 3
    await flush_outbox()
    for i in range(3):
        assert "doctor is unavailable" in wa.for_phone(phone(i))[-1].text
        assert [b[0].split(":")[0] for b in wa.for_phone(phone(i))[-1].buttons] == ["sc_resched", "sc_cancel"]
    assert await rows(SlotOffer) == []
    assert all(p.late_cancel_count == 0 for p in await rows(Patient))
    assert all(not x.is_late_cancel for x in await rows(Appointment))
    # the session can no longer be booked
    clock.set_now(ist("2026-09-28 07:00"))
    async with get_session() as db:
        assert await booking_service.find_slots(db, D, D, "morning") == []


async def test_patient_cancel_cutoff():
    tune(changes={"patient_cancel_cutoff_minutes": 120})
    a = await setup(1)
    clock.set_now(ist("2026-09-28 08:30"))
    with pytest.raises(change_service.TooLate):
        await change_service.cancel(a[0].id, by="patient", actor="patient")


async def test_reconciliation_deleted_event_cancels_and_busy_overlap_flags(cal, wa):
    a = await setup(2)
    await cal.delete((await get_appt(a[0].id)).gcal_event_id)
    await cal.add_busy_block(ist("2026-09-28 10:15"), ist("2026-09-28 10:45"))
    res = await reminders.reconcile_calendar()
    assert res == {"cancelled": 1, "flagged": 1}
    first = await get_appt(a[0].id)
    assert first.status == "cancelled" and first.cancel_reason_code == "calendar_deleted"
    assert (await get_appt(a[1].id)).needs_staff_decision == "calendar_conflict"
    assert (await get_appt(a[1].id)).status == "booked"  # never cancelled automatically
