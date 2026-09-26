"""Deterministic handling of every structured reply id (section 9), plus outbox retries."""
from datetime import timedelta

from app.db.models import Appointment, OutboxMessage, SlotOffer
from app.dev import clock
from app.scheduling import change_service, outbox
from tests.conftest import book, find_button, flush_outbox, get_appt, ist, make_patient, rows, send, tap

P = "+919800000001"


def last_text(wa, phone=P):
    return wa.for_phone(phone)[-1].text


async def test_menu_buttons(wa):
    p = await make_patient()
    await tap(P, "menu:my")
    assert "no upcoming" in last_text(wa)
    await book(p, "2026-09-28 11:00")
    await tap(P, "menu:my")
    assert "M-05" in last_text(wa) and "approx" in last_text(wa) or "11:00" in last_text(wa)
    await tap(P, "menu:info")
    assert "Dr. Example Clinic" in last_text(wa)


async def test_simple_acks(wa):
    p = await make_patient()
    a = await book(p, "2026-09-28 11:00")
    await tap(P, f"r2_onway:{a.id}")
    assert "see you soon" in last_text(wa)
    await tap(P, f"missed_coming:{a.id}")
    assert "see you soon" in last_text(wa)
    await tap(P, f"delay_ok:{a.id}")
    assert "We will see you soon" in last_text(wa)
    await tap(P, "fu_no:x")
    assert "whenever you would like" in last_text(wa)
    await tap(P, f"sc_cancel:{a.id}")
    await tap(P, f"cancel_no:{a.id}")
    assert "unchanged" in last_text(wa)
    await tap(P, "unknown_action:1")
    assert "no longer valid" in last_text(wa)
    await tap(P, "r2_onway:not-a-uuid")
    assert "no longer valid" in last_text(wa)


async def test_book_again_and_session_resched_start_booking(wa):
    await make_patient()
    await tap(P, "book_again:x")
    assert wa.for_phone(P)[-1].rows
    await tap(P, "sc_resched:x")
    assert wa.for_phone(P)[-1].rows


async def test_change_slot_resends_list(wa):
    await make_patient()
    await tap(P, "menu:book")
    await tap(P, find_button(wa, P, "slot:"))
    await tap(P, find_button(wa, P, "change_slot:"))
    assert wa.for_phone(P)[-1].rows


async def test_offer_buttons_via_chat(wa):
    ps = [await make_patient(f"+9198000000{i:02d}", f"P{i} X") for i in range(3)]
    a = [await book(p, f"2026-09-28 {t}") for p, t in zip(ps, ["10:00", "10:15", "10:30"])]
    await change_service.cancel(a[0].id, by="patient", actor="patient")
    await flush_outbox()
    b_phone, c_phone = "+919800000001", "+919800000002"
    c_first_offer = find_button(wa, c_phone, "offer_yes:")
    await tap(b_phone, find_button(wa, b_phone, "offer_yes:"))
    assert (await get_appt(a[1].id)).status == "rescheduled"
    assert "has moved to" in wa.for_phone(b_phone)[-1].text
    await tap(c_phone, c_first_offer)  # B already took 10:00
    assert "already been taken" in wa.for_phone(c_phone)[-1].text
    await tap(c_phone, find_button(wa, c_phone, "offer_yes:"))  # the cascade offer for B's old 10:15
    assert "has moved to" in wa.for_phone(c_phone)[-1].text
    await tap(c_phone, "offer_no:" + "0" * 32)
    assert "unchanged" in wa.for_phone(c_phone)[-1].text


async def test_waitlist_buttons_via_chat(wa, cal):
    from datetime import date

    from app.scheduling import offers

    ps = [await make_patient(f"+9198000000{i:02d}", f"P{i} X") for i in range(2)]
    a = await book(ps[0], "2026-09-28 10:00")
    await offers.join_waitlist(ps[1].id, date(2026, 9, 28))
    await change_service.cancel(a.id, by="patient", actor="patient")
    await flush_outbox()
    w = "+919800000001"
    await tap(w, find_button(wa, w, "wl_no:"))
    assert "still on the waitlist" in wa.for_phone(w)[-1].text


async def test_running_late_by_text(wa):
    p = await make_patient()
    a = await book(p, "2026-09-28 10:00")
    clock.set_now(ist("2026-09-28 09:40"))
    await send(P, "I will be 20 min late")
    assert (await get_appt(a.id)).patient_reported_late_minutes == 20
    assert "kept only until" in last_text(wa)
    await send(P, "sorry, running late by 90 minutes")
    assert "end of the session" in last_text(wa)


async def test_structured_reply_before_consent_is_gated(wa):
    await send(P, "Hi")
    await tap(P, "menu:book")
    assert [b[0] for b in wa.for_phone(P)[-1].buttons] == ["consent_yes", "consent_no"]


async def test_outbox_retries_then_fails(wa):
    p = await make_patient()
    await book(p, "2026-09-28 10:00")
    wa.fail_next = 100
    for i in range(6):
        await flush_outbox()
        clock.advance(60)
    row = (await rows(OutboxMessage))[0]
    assert row.status == "failed" and row.attempts == 6 and "SendFailed" in row.last_error
    wa.fail_next = 0


async def test_outbox_recovers_after_transient_failure(wa):
    p = await make_patient()
    await book(p, "2026-09-28 10:00")
    wa.fail_next = 1
    await flush_outbox()
    assert wa.for_phone(P) == []
    clock.advance(2)
    await flush_outbox()
    assert "confirmed" in last_text(wa)
