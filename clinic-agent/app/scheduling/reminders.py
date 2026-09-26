"""Background jobs: reminders, follow-ups, queue notices, offer expiry, reconciliation, retention."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.base import CalendarError, get_calendar
from app.calendar.slots import session_windows
from app.config import get_clinic
from app.db.models import Appointment, Conversation, FollowUp, Patient, ReminderLog
from app.db.session import get_session
from app.dev import clock
from app.scheduling import change_service, offers, outbox, queue_service
from app.scheduling.common import audit
from app.scheduling.notifications import claim_reminder
from app.timeutil import local_today

log = logging.getLogger(__name__)


def reminder_kind(minutes: int) -> str:
    return f"r{minutes // 60}h" if minutes % 60 == 0 else f"r{minutes}m"


TEMPLATE_FOR = {1440: "appt_reminder_24h", 120: "appt_reminder_2h"}


def _template_for(minutes: int) -> str:
    # any threshold of 6h+ uses the "day before" template, shorter ones the "day of" template
    return TEMPLATE_FOR.get(minutes) or ("appt_reminder_24h" if minutes >= 360 else "appt_reminder_2h")


async def run_appointment_reminders() -> int:
    cfg = get_clinic()
    now = clock.now()
    thresholds = sorted(cfg.reminders.before_minutes)
    if not thresholds:
        return 0
    horizon = now + timedelta(minutes=max(thresholds))
    sent = 0
    async with get_session() as db:
        appts = (
            await db.scalars(
                select(Appointment).where(
                    Appointment.status == "booked",
                    Appointment.is_walk_in.is_(False),
                    Appointment.start_utc > now,
                    Appointment.start_utc <= horizon,
                )
            )
        ).all()
        for a in appts:
            left = a.start_utc - now
            crossed = [m for m in thresholds if left <= timedelta(minutes=m)]
            if not crossed:
                continue
            m = min(crossed)  # only the most recent threshold; a missed earlier one is not sent late
            if a.created_at > a.start_utc - timedelta(minutes=m):
                continue  # booked less than the reminder interval before the appointment
            row = await claim_reminder(db, reminder_kind(m), appointment_id=a.id)
            if row is None:
                continue
            await outbox.notify(db, a.patient_id, _template_for(m), appointment_id=a.id, reminder_log_id=row.id)
            sent += 1
        await db.commit()
    if sent:
        outbox.kick()
    return sent


async def run_followups() -> int:
    """Daily 09:30 IST: first reminder on the due date, one nudge 3 days later, then stop."""
    today = local_today(clock.now())
    sent = 0
    async with get_session() as db:
        due = (
            await db.scalars(
                select(FollowUp).where(
                    or_(
                        (FollowUp.status == "pending") & (FollowUp.due_date <= today),
                        (FollowUp.status == "sent") & (FollowUp.due_date <= today - timedelta(days=3)),
                    )
                )
            )
        ).all()
        for fu in due:
            kind = "followup" if fu.status == "pending" else "followup2"
            row = await claim_reminder(db, kind, follow_up_id=fu.id)
            if row is None:
                continue
            fu.status = "sent"
            await outbox.notify(db, fu.patient_id, "followup_reminder", follow_up_id=fu.id, reminder_log_id=row.id)
            sent += 1
        await db.commit()
    if sent:
        outbox.kick()
    return sent


async def run_queue_jobs() -> None:
    """Queue notices for today's sessions, and auto-close sessions 30 min after scheduled end."""
    cfg = get_clinic()
    now = clock.now()
    today = local_today(now)
    for w in session_windows(cfg, today, include_holidays=True):
        async with get_session() as db:
            await queue_service.evaluate_notifications(db, today, w.name)
            await db.commit()
        if now >= w.end_utc + timedelta(minutes=cfg.queue.auto_close_after_minutes):
            async with get_session() as db:
                st = await queue_service.get_state(db, today, w.name)
                closed = st.closed_at is not None
                await db.commit()
            if not closed:
                await queue_service.close_session(today, w.name, "system")
    outbox.kick()


async def run_unconfirmed_autocancel() -> int:
    """Rule 10: patients at the no-show threshold must confirm the 24h reminder within 12 hours."""
    cfg = get_clinic()
    now = clock.now()
    n = 0
    async with get_session() as db:
        rows = (
            await db.execute(
                select(Appointment.id)
                .join(Patient, Patient.id == Appointment.patient_id)
                .join(ReminderLog, ReminderLog.appointment_id == Appointment.id)
                .where(
                    Appointment.status == "booked",
                    Appointment.start_utc > now,
                    Appointment.confirmed_by_patient_at.is_(None),
                    Patient.noshow_count >= cfg.queue.noshow_warn_threshold,
                    ReminderLog.kind == reminder_kind(max(cfg.reminders.before_minutes or [1440])),
                    ReminderLog.status == "sent",
                    ReminderLog.sent_at <= now - timedelta(hours=12),
                )
            )
        ).scalars().all()
    for appt_id in rows:
        try:
            await change_service.cancel(appt_id, by="system", actor="system", reason_code="unconfirmed")
            n += 1
        except change_service.DomainError:
            pass
    return n


async def reconcile_calendar() -> dict:
    """10.3 / 10B.6: deleted events -> staff cancel; overlapping busy blocks -> flag for staff."""
    cfg = get_clinic()
    now = clock.now()
    cal = get_calendar()
    result = {"cancelled": 0, "flagged": 0}
    async with get_session() as db:
        upcoming = (
            await db.scalars(
                select(Appointment).where(
                    Appointment.status == "booked",
                    Appointment.is_walk_in.is_(False),
                    Appointment.start_utc > now,
                    Appointment.start_utc <= now + timedelta(days=cfg.booking_horizon_days + 1),
                )
            )
        ).all()
        if not upcoming:
            return result
        try:
            busy = await cal.freebusy(min(a.start_utc for a in upcoming), max(a.end_utc for a in upcoming))
        except CalendarError:
            log.warning("reconciliation skipped: calendar unavailable")
            return result
        to_cancel = []
        for a in upcoming:
            if a.gcal_event_id:
                try:
                    ev = await cal.get(a.gcal_event_id)
                except CalendarError:
                    continue
                if ev is None:
                    to_cancel.append(a.id)
                    continue
            overlaps = any(a.start_utc < b1 and a.end_utc > b0 for b0, b1 in busy)
            if overlaps and a.needs_staff_decision != "calendar_conflict":
                a.needs_staff_decision = "calendar_conflict"
                result["flagged"] += 1
        await db.commit()
    for appt_id in to_cancel:
        try:
            await change_service.cancel(appt_id, by="staff", actor="system", reason_code="calendar_deleted")
            result["cancelled"] += 1
        except change_service.DomainError:
            pass
    # retry calendar events that never got created (e.g. patch fell back and failed)
    return result


async def purge_contexts() -> int:
    """Drop LLM context after 24h of inactivity (message storage rule)."""
    cutoff = clock.now() - timedelta(hours=24)
    async with get_session() as db:
        res = await db.execute(
            update(Conversation)
            .where(Conversation.updated_at < cutoff, Conversation.handoff_active.is_(False), Conversation.state != "IDLE")
            .values(context_json={}, state="IDLE")
        )
        res2 = await db.execute(
            update(Conversation).where(Conversation.updated_at < cutoff).values(context_json={})
        )
        await db.commit()
        return (res.rowcount or 0) + (res2.rowcount or 0)


async def anonymise_patient(db: AsyncSession, p: Patient, actor: str) -> None:
    from app.db.crypto import phone_hash
    import uuid

    p.name_enc = None
    p.phone_enc = None
    p.age = None
    p.phone_hash = phone_hash(f"+0deleted{uuid.uuid4().hex}")
    p.opted_out = True
    p.anonymised_at = clock.now()
    conv = await db.scalar(select(Conversation).where(Conversation.patient_id == p.id))
    if conv:
        conv.context_json = {}
        conv.state = "IDLE"
        conv.handoff_active = False
    for a in (await db.scalars(select(Appointment).where(Appointment.patient_id == p.id))).all():
        a.visit_reason_short_enc = None
    await audit(db, actor, "anonymise", "patient", p.id)


async def delete_patient_data(patient_id) -> None:
    """'DELETE MY DATA': cancel future appointments, anonymise, audit."""
    async with get_session() as db:
        future = (
            await db.scalars(
                select(Appointment.id).where(
                    Appointment.patient_id == patient_id,
                    Appointment.status == "booked",
                    Appointment.start_utc > clock.now(),
                )
            )
        ).all()
    for appt_id in future:
        try:
            await change_service.cancel(appt_id, by="staff", actor="system", reason_code="data_deletion")
        except change_service.DomainError:
            pass
    async with get_session() as db:
        # cancellation notices are pointless once the number is erased
        p = await db.get(Patient, patient_id)
        await anonymise_patient(db, p, "patient")
        await db.commit()


async def run_retention() -> int:
    """Monthly: anonymise patients with no activity for 2 years."""
    cutoff = clock.now() - timedelta(days=730)
    n = 0
    async with get_session() as db:
        stale = (
            await db.scalars(
                select(Patient).where(
                    Patient.anonymised_at.is_(None),
                    Patient.updated_at < cutoff,
                    or_(Patient.last_inbound_at.is_(None), Patient.last_inbound_at < cutoff),
                )
            )
        ).all()
        for p in stale:
            recent = await db.scalar(
                select(Appointment.id).where(Appointment.patient_id == p.id, Appointment.start_utc > cutoff)
            )
            if recent:
                continue
            await anonymise_patient(db, p, "system")
            n += 1
        await db.commit()
    return n


async def run_every_5_minutes() -> None:
    await run_appointment_reminders()
    await offers.expire_offers()
    await run_queue_jobs()
    await run_unconfirmed_autocancel()
    await outbox.process_outbox()


async def run_all_jobs() -> dict:
    """/dev/run-jobs: run everything now (uses the dev clock)."""
    out = {
        "reminders": await run_appointment_reminders(),
        "followups": await run_followups(),
        "offers_expired": await offers.expire_offers(),
    }
    await run_queue_jobs()
    out["autocancelled"] = await run_unconfirmed_autocancel()
    out["reconcile"] = await reconcile_calendar()
    out["outbox"] = await outbox.process_outbox()
    return out
