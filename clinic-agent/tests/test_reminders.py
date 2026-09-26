from datetime import date, timedelta

from app.db.models import Appointment, FollowUp, ReminderLog
from app.db.session import get_session
from app.dev import clock
from app.scheduling import reminders
from tests.conftest import book, find_button, flush_outbox, get_appt, ist, make_patient, rows, tap


async def test_24h_and_2h_each_sent_exactly_once(wa):
    clock.set_now(ist("2026-09-26 09:00"))
    p = await make_patient(inbound=False)
    a = await book(p, "2026-09-28 10:00")
    await flush_outbox()
    wa.clear()
    clock.set_now(ist("2026-09-27 10:05"))  # 23h55 before
    assert await reminders.run_appointment_reminders() == 1
    assert await reminders.run_appointment_reminders() == 0  # job run twice
    await flush_outbox()
    await flush_outbox()
    r24 = [m for m in wa.sent if m.template == "appt_reminder_24h"]
    assert len(r24) == 1 and "M-01" in r24[0].text
    assert [b[0].split(":")[0] for b in r24[0].buttons] == ["r24_confirm", "r24_resched", "r24_cancel"]
    clock.set_now(ist("2026-09-28 08:05"))
    assert await reminders.run_appointment_reminders() == 1
    assert await reminders.run_appointment_reminders() == 0
    await flush_outbox()
    r2 = [m for m in wa.sent if m.template == "appt_reminder_2h"]
    assert len(r2) == 1 and "10:00 AM–10:10 AM" in r2[0].text
    logs = await rows(ReminderLog, ReminderLog.appointment_id == a.id)
    assert sorted(l.kind for l in logs) == ["r24h", "r2h"] and all(l.status == "sent" for l in logs)


async def test_reminder_skipped_when_booked_inside_interval(wa):
    clock.set_now(ist("2026-09-27 12:00"))  # books 22h before: no 24h reminder
    p = await make_patient()
    await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-27 12:10"))
    assert await reminders.run_appointment_reminders() == 0
    clock.set_now(ist("2026-09-28 08:01"))
    assert await reminders.run_appointment_reminders() == 1  # the 2h one still goes


async def test_missed_24h_not_sent_late():
    clock.set_now(ist("2026-09-25 09:00"))
    p = await make_patient()
    a = await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-28 08:30"))  # the job was down for a day: only r2h
    await reminders.run_appointment_reminders()
    assert [l.kind for l in await rows(ReminderLog, ReminderLog.appointment_id == a.id)] == ["r2h"]


async def test_reminder_buttons(wa):
    clock.set_now(ist("2026-09-26 09:00"))
    p = await make_patient()
    a = await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-27 10:05"))
    await make_patient()  # refresh 24h window
    await reminders.run_appointment_reminders()
    await flush_outbox()
    await tap(p_phone := "+919800000001", find_button(wa, p_phone, "r24_confirm"))
    assert (await get_appt(a.id)).confirmed_by_patient_at is not None
    assert "confirmed" in wa.for_phone(p_phone)[-1].text
    await tap(p_phone, find_button(wa, p_phone, "r24_cancel"))
    await tap(p_phone, find_button(wa, p_phone, "cancel_yes"))
    assert (await get_appt(a.id)).status == "cancelled"


async def test_2h_running_late_flow(wa):
    clock.set_now(ist("2026-09-26 09:00"))
    p = await make_patient()
    a = await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-28 08:05"))
    await make_patient()
    await reminders.run_appointment_reminders()
    await flush_outbox()
    await tap("+919800000001", find_button(wa, "+919800000001", "r2_late"))
    await tap("+919800000001", find_button(wa, "+919800000001", "late_min:"), "About 20 min")
    got = await get_appt(a.id)
    assert got.queue_status == "running_late" and got.patient_reported_late_minutes in (10, 20, 30, 45)
    assert "kept only until 10 minutes" in wa.for_phone("+919800000001")[-1].text


async def test_followup_due_then_nudge_then_stop(wa):
    p = await make_patient(inbound=False)
    async with get_session() as db:
        fu = FollowUp(patient_id=p.id, due_date=date(2026, 10, 5), note_for_patient="follow-up visit")
        db.add(fu)
        await db.commit()
    clock.set_now(ist("2026-10-04 09:30"))
    assert await reminders.run_followups() == 0
    clock.set_now(ist("2026-10-05 09:30"))
    assert await reminders.run_followups() == 1
    assert await reminders.run_followups() == 0
    clock.set_now(ist("2026-10-08 09:30"))
    assert await reminders.run_followups() == 1  # nudge
    clock.set_now(ist("2026-10-11 09:30"))
    assert await reminders.run_followups() == 0  # then stop
    await flush_outbox()
    sent = [m for m in wa.sent if m.template == "followup_reminder"]
    assert len(sent) == 2 and "Dr. Example" in sent[0].text


async def test_followup_book_button_marks_booked(wa):
    p = await make_patient()
    async with get_session() as db:
        fu = FollowUp(patient_id=p.id, due_date=date(2026, 9, 29))
        db.add(fu)
        await db.commit()
        fid = fu.id
    await tap("+919800000001", f"fu_book:{fid}")
    await tap("+919800000001", find_button(wa, "+919800000001", "slot:"))
    await tap("+919800000001", find_button(wa, "+919800000001", "confirm_book:"))
    assert (await rows(FollowUp))[0].status == "booked"


async def test_unconfirmed_autocancel_for_repeat_no_shows(wa):
    clock.set_now(ist("2026-09-26 09:00"))
    p = await make_patient()
    from app.db.models import Patient

    async with get_session() as db:
        (await db.get(Patient, p.id)).noshow_count = 2
        await db.commit()
    a = await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-27 10:05"))
    await make_patient()
    await reminders.run_appointment_reminders()
    await flush_outbox()
    assert "within 12 hours" in [m for m in wa.sent if "Reminder" in m.text][-1].text
    clock.set_now(ist("2026-09-27 22:10"))
    assert await reminders.run_unconfirmed_autocancel() == 1
    assert (await get_appt(a.id)).cancel_reason_code == "unconfirmed"


async def test_opted_out_patient_gets_no_reminder(wa):
    clock.set_now(ist("2026-09-26 09:00"))
    p = await make_patient(opted_out=True)
    await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-27 10:05"))
    await reminders.run_appointment_reminders()
    await flush_outbox()
    assert wa.for_phone("+919800000001") == []
    assert (await rows(ReminderLog))[0].status == "blocked"


async def test_retention_and_context_purge():
    from app.db.models import Conversation, Patient

    p = await make_patient()
    async with get_session() as db:
        db.add(Conversation(patient_id=p.id, state="CHOOSING_SLOT", context_json={"turns": [{"role": "user", "text": "x"}]}))
        await db.commit()
    clock.advance(minutes=25 * 60)
    await reminders.purge_contexts()
    c = (await rows(Conversation))[0]
    assert c.context_json == {} and c.state == "IDLE"
    clock.advance(minutes=731 * 24 * 60)
    assert await reminders.run_retention() == 1
    pp = (await rows(Patient))[0]
    assert pp.anonymised_at and pp.name_enc is None and pp.phone_enc is None
