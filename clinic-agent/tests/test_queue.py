"""Section 10A rules on the real queue_service (clock-driven, no sleeping)."""
import asyncio
from datetime import date

from app.db.models import Appointment, Patient, ReminderLog
from app.dev import clock
from app.scheduling import change_service, queue_service as Q
from app.timeutil import to_local
from tests.conftest import book, flush_outbox, get_appt, get_patient, ist, make_patient, rows, tune

D = date(2026, 9, 28)
TIMES = ["10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30", "11:45"]


async def setup(n=5):
    out = []
    for i in range(n):
        p = await make_patient(f"+9198000000{i:02d}", f"Pat{i} X")
        out.append(await book(p, f"2026-09-28 {TIMES[i]}"))
    return out


async def order():
    async with __import__("app.db.session", fromlist=["x"]).get_session() as db:
        appts = await Q.load_session(db, D, "morning")
    return [a.token_label for a in sorted((a for a in appts if a.queue_status in Q.WAITING), key=Q.order_key)]


def at(hm):
    clock.set_now(ist(f"2026-09-28 {hm}"))


async def status(a):
    return (await get_appt(a.id)).queue_status


async def test_tokens_from_slot_position_stable_after_cancel():
    a = await setup(4)
    assert [x.token_label for x in a] == ["M-01", "M-02", "M-03", "M-04"]
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    assert [(await get_appt(x.id)).token_label for x in a[1:]] == ["M-02", "M-03", "M-04"]


async def test_absent_within_grace_keeps_position_and_is_called_next():
    tune(queue={"grace_minutes": 30})
    a = await setup(3)
    at("10:10")
    await Q.check_in(a[1].id, "t")  # B arrives
    at("10:20")
    await Q.check_in(a[2].id, "t")  # C arrives early
    res = await Q.call_next(D, "morning", "t")
    assert res.appointment.id == a[1].id and await status(a[0]) == "scheduled"  # A not skipped
    at("10:25")
    await Q.check_in(a[0].id, "t")  # A arrives within grace: back in their place
    assert await status(a[0]) == "checked_in"
    at("10:35")
    res = await Q.call_next(D, "morning", "t")
    assert res.appointment.id == a[0].id  # A before C


async def test_absent_past_grace_is_skipped_and_next_checked_in_called(wa):
    a = await setup(2)
    at("10:12")
    await Q.check_in(a[1].id, "t")
    at("10:16")
    res = await Q.call_next(D, "morning", "t")
    assert res.appointment.id == a[1].id
    assert await status(a[0]) == "skipped"
    await flush_outbox()
    assert "we called your turn" in wa.for_phone("+919800000000")[-1].text
    # calling again does not send a second missed-turn message
    await Q.call_next(D, "morning", "t")
    missed = await rows(ReminderLog, ReminderLog.kind == "missed")
    assert len(missed) == 1


async def test_late_patient_reinserted_after_exactly_n_waiting():
    a = await setup(5)  # M-01..M-05
    at("10:05")
    for x in a[1:]:
        await Q.check_in(x.id, "t")
    at("10:16")
    await Q.call_next(D, "morning", "t")  # A skipped, B called
    assert await status(a[0]) == "skipped"
    at("10:25")
    res = await Q.check_in(a[0].id, "t")  # 25 min late <= 45
    assert await order() == ["M-03", "M-04", "M-01", "M-05"]
    assert (await get_appt(a[4].id)).displacement_count == 1
    assert (await get_appt(a[2].id)).displacement_count == 0
    assert res.note is None


async def test_fewer_waiting_than_n_goes_after_last():
    a = await setup(3)
    at("10:05")
    await Q.check_in(a[1].id, "t")
    at("10:16")
    await Q.call_next(D, "morning", "t")  # A skipped, B in consultation, nobody waiting
    at("10:20")
    await Q.check_in(a[0].id, "t")
    assert (await order())[0] == "M-01"  # right after the current consultation (C not arrived yet)


async def test_very_late_goes_to_end_and_reception_gets_choice():
    a = await setup(5)
    at("10:05")
    for x in a[1:]:
        await Q.check_in(x.id, "t")
    at("10:16")
    await Q.call_next(D, "morning", "t")
    at("12:20")
    res = await Q.check_in(a[0].id, "t")  # 140 min late
    assert (await order())[-1] == "M-01"
    assert res.note == "late_overrun" and (await get_appt(a[0].id)).needs_staff_decision == "late_overrun"


async def test_very_late_without_overrun_no_choice():
    a = await setup(2)
    at("10:05")
    await Q.check_in(a[1].id, "t")
    at("10:16")
    await Q.call_next(D, "morning", "t")
    at("11:00")
    res = await Q.check_in(a[0].id, "t")  # 60 min late, but the session has room
    assert res.note is None and (await order())[-1] == "M-01"


async def test_displacement_cap():
    tune(queue={"late_reinsert_after": 1, "max_displacements_per_patient": 2})
    a = await setup(6)  # A B C D E F
    at("09:55")
    await Q.check_in(a[4].id, "t")  # E is on time and waiting
    await Q.check_in(a[5].id, "t")  # F waiting
    await Q.check_in(a[3].id, "t")  # D waiting (ahead of E)
    at("10:41")
    await Q.call_next(D, "morning", "t")  # A, B, C skipped (past grace); D called
    for x in a[:3]:
        assert await status(x) == "skipped"
    # A, B, C arrive late one after the other; each goes after 1 waiting patient (E) -> ahead of F
    at("10:42")  # A is 42 min late (<= 45): re-inserted, not moved to the end
    await Q.check_in(a[0].id, "t")
    await Q.check_in(a[1].id, "t")
    f = await get_appt(a[5].id)
    assert f.displacement_count == 2
    await Q.check_in(a[2].id, "t")
    assert (await get_appt(a[5].id)).displacement_count == 2  # cap reached: C is placed after F
    assert (await order())[-1] == "M-03"


async def test_early_arrival_does_not_jump_unless_everyone_ahead_skipped():
    a = await setup(2)
    at("09:50")
    await Q.check_in(a[1].id, "t")  # B early (slot 10:15)
    at("10:05")
    res = await Q.call_next(D, "morning", "t")
    assert res.appointment is None and await status(a[0]) == "scheduled"
    at("10:11")
    res = await Q.call_next(D, "morning", "t")  # A now past grace -> skipped -> B may go early
    assert await status(a[0]) == "skipped" and res.appointment.id == a[1].id


async def test_doctor_delay_shifts_eta_notifies_once_and_nobody_marked_late(wa):
    a = await setup(3)
    at("09:00")
    await Q.set_delay(D, "morning", 40, "t")
    async with __import__("app.db.session", fromlist=["x"]).get_session() as db:
        _, etas = await Q.session_etas(db, D, "morning")
    assert to_local(etas[a[0].id].window_start).strftime("%H:%M") == "10:40"
    delays = await rows(ReminderLog, ReminderLog.kind.like("delay_%"))
    assert len(delays) == 3
    await Q.set_delay(D, "morning", 40, "t")  # same delay again: no new notices
    assert len(await rows(ReminderLog, ReminderLog.kind.like("delay_%"))) == 3
    await flush_outbox()
    assert "running late" in wa.for_phone("+919800000000")[-1].text
    at("10:45")  # A is 45 min after slot but the doctor only starts now
    await Q.check_in(a[1].id, "t")
    res = await Q.call_next(D, "morning", "t")
    assert await status(a[0]) == "scheduled"  # not skipped: grace counts from the delayed start
    assert res.appointment.id == a[1].id


async def test_eta_never_earlier_than_slot_and_earlier_sends_nothing():
    a = await setup(3)
    at("09:30")
    await Q.check_in(a[2].id, "t")
    async with __import__("app.db.session", fromlist=["x"]).get_session() as db:
        _, etas = await Q.session_etas(db, D, "morning")
    for x in a:
        assert etas[x.id].start >= x.start_utc and etas[x.id].window_start >= x.start_utc
    await change_service.cancel(a[0].id, by="patient", actor="patient")  # queue moves up
    await change_service.cancel(a[1].id, by="patient", actor="patient")
    assert await rows(ReminderLog, ReminderLog.kind.like("delay_%")) == []


async def test_no_show_on_close_counts_once(wa):
    a = await setup(2)
    at("10:05")
    await Q.check_in(a[1].id, "t")
    at("13:10")
    n1 = await Q.close_session(D, "morning", "t")
    n2 = await Q.close_session(D, "morning", "t")
    assert (n1, n2) == (1, 0)
    assert (await get_patient((await get_appt(a[0].id)).patient_id)).noshow_count == 1
    assert (await get_appt(a[0].id)).status == "no_show"
    await flush_outbox()
    assert any("we missed you" in m.text for m in wa.for_phone("+919800000000"))


async def test_two_concurrent_call_next_call_one_patient():
    a = await setup(3)
    at("09:55")
    for x in a:
        await Q.check_in(x.id, "t")
    at("10:00")
    r1, r2 = await asyncio.gather(
        Q.call_next(D, "morning", "t1", expected_current="none"),
        Q.call_next(D, "morning", "t2", expected_current="none"),
    )
    called = [r for r in (r1, r2) if r.changed]
    assert len(called) == 1
    in_consult = await rows(Appointment, Appointment.queue_status == "in_consultation")
    assert len(in_consult) == 1 and in_consult[0].id == a[0].id


async def test_priority_moves_patient_first_and_is_audited():
    a = await setup(3)
    at("09:55")
    for x in a:
        await Q.check_in(x.id, "t")
    await Q.set_priority(a[2].id, "admin:reception")
    assert (await order())[0] == "M-03"
    from app.db.models import QueueEvent

    ev = await rows(QueueEvent, QueueEvent.event == "priority_set")
    assert ev[0].actor == "admin:reception"


async def test_walk_in_never_takes_on_time_position():
    a = await setup(3)
    at("09:55")
    await Q.check_in(a[0].id, "t")
    await Q.check_in(a[1].id, "t")
    at("10:02")
    w = await Q.add_walk_in(D, "morning", "t", name="Walk In")
    assert w.token_label == "W-01"
    assert await order() == ["M-01", "M-02", "W-01", "M-03"] or await order() == ["M-01", "W-01", "M-02", "M-03"]
    # M-02 is already waiting so the walk-in goes after them
    assert (await order()).index("W-01") > (await order()).index("M-02")


async def test_running_late_report_keeps_turn_within_grace():
    a = await setup(2)
    at("09:30")
    await Q.report_running_late(a[0].id, 5)
    assert await status(a[0]) == "running_late"
    at("10:08")
    await Q.check_in(a[0].id, "t")
    assert await status(a[0]) == "checked_in" and (await order())[0] == "M-01"


async def test_queue_status_for_patient():
    a = await setup(3)
    at("09:55")
    for x in a:
        await Q.check_in(x.id, "t")
    at("10:00")
    await Q.call_next(D, "morning", "t")
    from app.db.session import get_session

    async with get_session() as db:
        info = await Q.queue_status_for(db, a[2].patient_id)
    assert info["token"] == "M-03" and info["ahead"] == 2 and info["current"] == "M-01" and "–" in info["eta"]
