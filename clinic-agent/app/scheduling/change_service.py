"""Cancellations and reschedules with all side effects (section 10B).

Every public operation is one transaction holding the session lock(s) and slot lock(s).
Calendar calls and WhatsApp messages go through the outbox after commit.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.slots import session_of_slot
from app.config import get_clinic
from app.db.models import Appointment, Patient, SlotOffer
from app.db.session import get_session, hold, session_key, slot_key
from app.dev import clock
from app.scheduling import booking_service, offers, outbox, queue_service
from app.scheduling.common import DomainError, audit, qevent
from app.timeutil import local_today


class NotAllowed(DomainError):
    code = "cancel.not_allowed"


class TooLate(DomainError):
    code = "cancel.too_late"


class NotFound(DomainError):
    code = "stale"


BLOCKED_FOR_PATIENT = ("checked_in", "in_consultation", "done")


def _check_patient_may_change(a: Appointment) -> None:
    if a.queue_status in BLOCKED_FOR_PATIENT:
        raise NotAllowed()
    cutoff = get_clinic().changes.patient_cancel_cutoff_minutes
    if a.start_utc - clock.now() < timedelta(minutes=cutoff):
        raise TooLate()


async def cancel_locked(
    db: AsyncSession,
    a: Appointment,
    *,
    by: str,
    actor: str,
    reason_code: str | None = None,
    event: str = "cancelled",
    new_status: str = "cancelled",
    delete_event: bool = True,
    count_late: bool = True,
) -> None:
    """Steps 1-6 and 9 of 10B.1. Caller holds locks, handles notifications/offers and commits."""
    cfg = get_clinic()
    now = clock.now()
    a.status = new_status
    a.queue_status = "cancelled"
    a.cancelled_at = now
    a.cancelled_by = by
    a.cancel_reason_code = reason_code
    if count_late and by == "patient" and a.start_utc - now < timedelta(minutes=cfg.changes.late_cancel_minutes):
        a.is_late_cancel = True
        patient = await db.get(Patient, a.patient_id)
        patient.late_cancel_count += 1
    if delete_event and a.gcal_event_id:
        await outbox.gcal(db, "delete", dedupe_key=f"gcal_delete:{a.gcal_event_id}", event_id=a.gcal_event_id)
    # open offers made *to* this appointment are void now
    for o in (
        await db.scalars(
            select(SlotOffer).where(SlotOffer.offered_to_appointment_id == a.id, SlotOffer.status == "open")
        )
    ).all():
        o.status = "superseded"
    await qevent(db, a, event, actor, reason=reason_code)
    await audit(db, actor, event, "appointment", a.id, reason=reason_code)
    await db.flush()


async def _after_change(db: AsyncSession, sessions: set[tuple[date, str]], actor: str) -> None:
    await booking_service.renumber_sequential(db, list(sessions), actor)
    today = local_today(clock.now())
    for d, s in sessions:
        if d == today:
            await queue_service.evaluate_notifications(db, d, s)


async def cancel(appointment_id: uuid.UUID, *, by: str, actor: str, reason_code: str | None = None,
                 patient_id: uuid.UUID | None = None) -> Appointment:
    async with get_session() as db:
        a = await db.get(Appointment, appointment_id)
        if a is None or (patient_id is not None and a.patient_id != patient_id):
            raise NotFound()
        async with hold(db, session_key(a.session_date, a.session), slot_key(a.start_utc)):
            await db.refresh(a)
            if a.status != "booked":
                raise NotFound()
            if by == "patient":
                _check_patient_may_change(a)
            await cancel_locked(db, a, by=by, actor=actor, reason_code=reason_code)
            await outbox.notify(db, a.patient_id, "appt_cancelled", dedupe_key=f"cancelled:{a.id}", appointment_id=a.id)
            if not a.is_walk_in:
                await offers.start_freed_slot_flow(db, a.start_utc, source_appointment_id=a.id, depth=0)
            await _after_change(db, {(a.session_date, a.session)}, actor)
            await db.commit()
    outbox.kick()
    return a


async def reschedule_locked(
    db: AsyncSession,
    old: Appointment,
    new_start: datetime,
    *,
    by: str,
    actor: str,
    cascade_depth: int = 0,
    via_offer: bool = False,
) -> Appointment:
    """10B.2. Caller holds session + slot locks for both old and new slots."""
    patient = await db.get(Patient, old.patient_id)
    # 1-2: book the new slot first; failure leaves the old appointment untouched
    new = await booking_service.book_locked(
        db, patient, new_start, source=old.source, actor=actor, rescheduled_from=old
    )
    # 3: cancel steps on the old one, without appt_cancelled; keep the calendar event (patched)
    await cancel_locked(
        db, old, by=by, actor=actor, reason_code="rescheduled", event="rescheduled_out",
        new_status="rescheduled", delete_event=False, count_late=False,
    )
    await qevent(db, new, "moved_earlier" if via_offer else "rescheduled_in", actor, from_token=old.token_label)
    # 5: one message with new date, token, expected time, report-by
    await outbox.notify(db, new.patient_id, "appt_rescheduled", dedupe_key=f"rescheduled:{new.id}", appointment_id=new.id)
    # freed slot flow for the old slot (8)
    await offers.start_freed_slot_flow(db, old.start_utc, source_appointment_id=old.id, depth=cascade_depth)
    await _after_change(db, {(old.session_date, old.session), (new.session_date, new.session)}, actor)
    return new


async def reschedule(appointment_id: uuid.UUID, new_start: datetime, *, by: str, actor: str,
                     patient_id: uuid.UUID | None = None) -> Appointment:
    cfg = get_clinic()
    async with get_session() as db:
        old = await db.get(Appointment, appointment_id)
        if old is None or (patient_id is not None and old.patient_id != patient_id):
            raise NotFound()
        nd, ns = session_of_slot(cfg, new_start)
        keys = (
            session_key(old.session_date, old.session), session_key(nd, ns), slot_key(old.start_utc), slot_key(new_start)
        )
        async with hold(db, *keys):
            await db.refresh(old)
            if old.status != "booked":
                raise NotFound()
            if by == "patient":
                _check_patient_may_change(old)
            new = await reschedule_locked(db, old, new_start, by=by, actor=actor)
            await db.commit()
    outbox.kick()
    return new


async def cancel_session_by_clinic(d: date, session: str, actor: str) -> int:
    """10B.6: doctor unavailable. No offers, no late-cancel counts."""
    n = 0
    async with get_session() as db:
        async with hold(db, session_key(d, session)):
            st = await queue_service.get_state(db, d, session)
            st.cancelled_by_clinic = True
            appts = await queue_service.load_session(db, d, session)
            for a in appts:
                if a.status != "booked":
                    continue
                await cancel_locked(db, a, by="staff", actor=actor, reason_code="clinic_closed", count_late=False)
                if not a.is_walk_in:
                    await outbox.notify(
                        db, a.patient_id, "session_cancelled_by_clinic", dedupe_key=f"sc:{a.id}", appointment_id=a.id
                    )
                n += 1
            for o in (
                await db.scalars(
                    select(SlotOffer).where(
                        SlotOffer.session_date == d, SlotOffer.session == session, SlotOffer.status == "open"
                    )
                )
            ).all():
                o.status = "superseded"
            await audit(db, actor, "cancel_session", "session", f"{d}:{session}", count=n)
            await db.commit()
    outbox.kick()
    return n
