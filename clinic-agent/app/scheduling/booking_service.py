"""Booking with locking (section 10.2) and slot lookup."""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar import slots as slotlib
from app.calendar.base import CalendarError, get_calendar
from app.config import get_clinic
from app.db.crypto import encrypt
from app.db.models import Appointment, Patient, SessionState
from app.db.session import get_session, hold, session_key, slot_key
from app.dev import clock
from app.scheduling import outbox, tokens
from app.scheduling.common import DomainError, audit, calendar_summary, patient_name, qevent, short_ref
from app.timeutil import local_to_utc, local_today, to_local

log = logging.getLogger(__name__)


class SlotTaken(DomainError):
    code = "slots.retry"


class BookingLimit(DomainError):
    code = "book.limit"


class InvalidSlot(DomainError):
    code = "stale"


class CalendarUnavailable(DomainError):
    code = "book.failed"


# ------------------------------------------------------------------- slot lookup


async def closed_sessions(db: AsyncSession, date_from: date, date_to: date) -> set[tuple[date, str]]:
    rows = (
        await db.execute(
            select(SessionState.session_date, SessionState.session).where(
                SessionState.session_date >= date_from,
                SessionState.session_date <= date_to,
                (SessionState.cancelled_by_clinic.is_(True)) | (SessionState.closed_at.is_not(None)),
            )
        )
    ).all()
    return {(r[0], r[1]) for r in rows}


async def booked_starts(db: AsyncSession, start: datetime, end: datetime) -> set[datetime]:
    rows = await db.scalars(
        select(Appointment.start_utc).where(
            Appointment.status == "booked",
            Appointment.is_walk_in.is_(False),
            Appointment.start_utc >= start,
            Appointment.start_utc < end,
        )
    )
    return set(rows.all())


async def find_slots(
    db: AsyncSession,
    date_from: date,
    date_to: date,
    part_of_day: str | None = None,
    not_before: time | None = None,
    limit: int | None = None,
) -> list[datetime]:
    cfg = get_clinic()
    now = clock.now()
    range_start = local_to_utc(date_from, time(0, 0))
    range_end = local_to_utc(date_to + timedelta(days=1), time(0, 0))
    busy = await get_calendar().freebusy(range_start, range_end)
    booked = await booked_starts(db, range_start, range_end)
    closed = await closed_sessions(db, date_from, date_to)
    out = slotlib.available_slots(cfg, date_from, date_to, now, busy, booked, part_of_day, not_before)
    out = [s for s in out if slotlib.session_of_slot(cfg, s) not in closed]
    return out[:limit] if limit else out


async def active_bookings(db: AsyncSession, patient_id, exclude_id=None) -> list[Appointment]:
    q = select(Appointment).where(
        Appointment.patient_id == patient_id,
        Appointment.status == "booked",
        Appointment.start_utc > clock.now() - timedelta(hours=3),
    )
    if exclude_id:
        q = q.where(Appointment.id != exclude_id)
    return list((await db.scalars(q.order_by(Appointment.start_utc))).all())


# ----------------------------------------------------------------------- booking


async def book(
    patient_id: uuid.UUID,
    start_utc: datetime,
    *,
    name: str | None = None,
    age: int | None = None,
    reason: str | None = None,
    source: str = "whatsapp",
    actor: str = "patient",
    enforce_limit: bool = True,
) -> Appointment:
    """Public entry point: own transaction, locks, commit, then kick the outbox."""
    d, session = slotlib.session_of_slot(get_clinic(), start_utc)
    if name or age is not None:
        # separate short transaction: book_locked() must not hold a write lock while it calls the
        # calendar (matters for SQLite dev setups where the fake calendar shares the database)
        async with get_session() as db:
            patient = await db.get(Patient, patient_id)
            if name:
                patient.name_enc = encrypt(name.strip()[:80])
            if age is not None:
                patient.age = age
            await db.commit()
    async with get_session() as db:
        async with hold(db, slot_key(start_utc), session_key(d, session)):
            patient = await db.get(Patient, patient_id)
            appt = await book_locked(
                db, patient, start_utc, reason=reason, source=source, actor=actor, enforce_limit=enforce_limit
            )
            await renumber_sequential(db, [(appt.session_date, appt.session)], actor, new_ids={appt.id})
            await outbox.notify(db, patient.id, "appt_confirmation", dedupe_key=f"confirm:{appt.id}", appointment_id=appt.id)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                await _compensate_event(appt)
                raise SlotTaken() from exc
    outbox.kick()
    return appt


async def _compensate_event(appt: Appointment) -> None:
    if appt.gcal_event_id:
        try:
            await get_calendar().delete(appt.gcal_event_id)
        except CalendarError:
            log.warning("could not remove calendar event after failed booking")


async def book_locked(
    db: AsyncSession,
    patient: Patient,
    start_utc: datetime,
    *,
    reason: str | None = None,
    source: str = "whatsapp",
    actor: str = "patient",
    enforce_limit: bool = True,
    rescheduled_from: Appointment | None = None,
) -> Appointment:
    """Steps 2-5 of 10.2. Caller holds the slot + session locks and commits.
    Must be called before the transaction has written anything (see book())."""
    cfg = get_clinic()
    now = clock.now()
    if not slotlib.is_valid_slot_start(cfg, start_utc) or start_utc <= now:
        raise InvalidSlot()
    d, session = slotlib.session_of_slot(cfg, start_utc)
    if (d, session) in await closed_sessions(db, d, d):
        raise SlotTaken()
    end_utc = start_utc + timedelta(minutes=cfg.slot_minutes)

    taken = await db.scalar(
        select(Appointment.id).where(
            Appointment.start_utc == start_utc, Appointment.status == "booked", Appointment.is_walk_in.is_(False)
        )
    )
    if taken:
        raise SlotTaken()
    try:
        busy = await get_calendar().freebusy(start_utc, end_utc)
    except CalendarError as exc:
        raise CalendarUnavailable() from exc
    if slotlib.remove_busy(cfg, [start_utc], busy) == []:
        raise SlotTaken()

    if enforce_limit:
        active = await active_bookings(db, patient.id, exclude_id=rescheduled_from.id if rescheduled_from else None)
        if len(active) >= cfg.max_active_bookings_per_patient:
            raise BookingLimit(max=cfg.max_active_bookings_per_patient)

    sd, sess, slot_no, slot_label = tokens.slot_token(cfg, start_utc)
    token_no, token_label = slot_no, slot_label
    if cfg.changes.token_mode == "sequential":
        if tokens.tokens_frozen(cfg, sd, now):
            current_max = await db.scalar(
                select(func.max(Appointment.token_no)).where(
                    Appointment.session_date == sd,
                    Appointment.session == sess,
                    Appointment.is_walk_in.is_(False),
                    Appointment.status.not_in(("cancelled", "rescheduled")),
                )
            )
            token_no = (current_max or 0) + 1
            token_label = tokens.label(tokens.session_prefix(cfg, sess), token_no)
        else:  # final number assigned by renumber_sequential()
            token_no, token_label = 0, f"{tokens.session_prefix(cfg, sess)}-T{uuid.uuid4().hex[:8]}"

    appt_id = uuid.uuid4()
    event_id = None
    if rescheduled_from is not None and rescheduled_from.gcal_event_id:
        event_id = rescheduled_from.gcal_event_id  # patched after commit (keeps one event)
        await outbox.gcal(db, "patch", event_id=event_id, appointment_id=appt_id)
    else:
        try:
            event_id = await get_calendar().insert(
                str(appt_id),
                calendar_summary(patient_name(patient)),
                f"Booked via WhatsApp. Ref: {short_ref(appt_id)}",
                start_utc,
                end_utc,
            )
        except CalendarError as exc:
            raise CalendarUnavailable() from exc

    appt = Appointment(
        id=appt_id,
        patient_id=patient.id,
        start_utc=start_utc,
        end_utc=end_utc,
        status="booked",
        visit_reason_short_enc=encrypt(reason.strip()[:60]) if reason else (
            rescheduled_from.visit_reason_short_enc if rescheduled_from else None
        ),
        gcal_event_id=event_id,
        source=source,
        session_date=sd,
        session=sess,
        token_no=token_no,
        token_label=token_label,
        queue_status="scheduled",
        queue_position=float(slot_no),
        last_eta_sent_local=start_utc,
        rescheduled_from_id=rescheduled_from.id if rescheduled_from else None,
        previous_token_label=rescheduled_from.token_label if rescheduled_from else None,
    )
    db.add(appt)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        if rescheduled_from is None:
            await _compensate_event(appt)
        raise SlotTaken() from exc
    await audit(db, actor, "book" if rescheduled_from is None else "reschedule_in", "appointment", appt.id, source=source)
    if rescheduled_from is None:
        await qevent(db, appt, "booked", actor)
    return appt


# ---------------------------------------------------------- sequential renumbering


async def renumber_sequential(
    db: AsyncSession, sessions: list[tuple[date, str]], actor: str, new_ids: set | None = None
) -> None:
    """10B.3: tokens 1..N with no gaps, until the freeze time. No-op in slot mode."""
    cfg = get_clinic()
    if cfg.changes.token_mode != "sequential":
        return
    now = clock.now()
    new_ids = new_ids or set()
    prefix_cache = {}
    for sd, sess in set(sessions):
        if tokens.tokens_frozen(cfg, sd, now):
            continue
        live = list(
            (
                await db.scalars(
                    select(Appointment)
                    .where(
                        Appointment.session_date == sd,
                        Appointment.session == sess,
                        Appointment.status == "booked",
                        Appointment.is_walk_in.is_(False),
                    )
                    .order_by(Appointment.start_utc)
                )
            ).all()
        )
        prefix = prefix_cache.setdefault(sess, tokens.session_prefix(cfg, sess))
        wanted = tokens.sequential_tokens([a.start_utc for a in live])
        changed = [a for a in live if a.token_no != wanted[a.start_utc]]
        if not changed:
            continue
        # two-phase update so the unique (session, token_label) index never sees a clash
        for a in changed:
            a.token_label = f"{prefix}-T{uuid.uuid4().hex[:8]}"
        await db.flush()
        for a in changed:
            old_no = a.token_no
            a.token_no = wanted[a.start_utc]
            a.token_label = tokens.label(prefix, a.token_no)
            if a.id in new_ids or old_no == 0:
                continue
            old_label = tokens.label(prefix, old_no)
            a.token_version += 1
            await qevent(db, a, "token_renumbered", actor, old=old_label, new=a.token_label)
            bucket = now.strftime("%Y%m%d%H")
            await outbox.notify(
                db, a.patient_id, "token_updated", dedupe_key=f"token_updated:{a.id}:{bucket}",
                appointment_id=a.id, old_token=old_label,
            )
        await db.flush()


def today_local() -> date:
    return local_today(clock.now())


def local_date_of(dt: datetime) -> date:
    return to_local(dt).date()
