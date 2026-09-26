"""Live queue: check-in, call next, skip, late re-insertion, priority, delay, close, ETA (section 10A)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.slots import SessionWindow, session_window
from app.config import ClinicConfig, get_clinic
from app.db.crypto import encrypt, phone_hash
from app.db.models import Appointment, Patient, ReminderLog, SessionState
from app.db.session import get_session, hold, session_key
from app.dev import clock
from app.scheduling import outbox, tokens
from app.scheduling.common import DomainError, audit, get_or_create_patient, qevent
from app.scheduling.notifications import claim_reminder, eta_text
from app.timeutil import ceil_to, local_today

WAITING = ("scheduled", "running_late", "checked_in")
LIVE = WAITING + ("in_consultation",)
ANY = "__any__"
NOBODY = "none"  # expected_current value meaning "the board showed nobody with the doctor"


class QueueError(DomainError):
    code = "stale"


@dataclass
class Eta:
    start: datetime  # unrounded expected start
    window_start: datetime
    window_end: datetime
    ahead: int

    @property
    def end(self) -> datetime:
        return self.window_end


@dataclass
class ActionResult:
    appointment: Appointment | None = None
    note: str | None = None  # e.g. "late_overrun" -> reception gets a choice
    changed: bool = True


# ------------------------------------------------------------------ pure helpers


def order_key(a: Appointment):
    return (not a.priority, a.queue_position, a.start_utc)


def avg_consult_minutes(cfg: ClinicConfig, appts: list[Appointment]) -> float:
    done = [
        a
        for a in appts
        if a.consult_started_at and a.consult_ended_at and a.queue_status == "done"
    ]
    done.sort(key=lambda a: a.consult_ended_at)
    recent = done[-cfg.queue.eta_rolling_window :]
    if len(recent) < 2:
        return float(cfg.slot_minutes)
    avg = sum((a.consult_ended_at - a.consult_started_at).total_seconds() / 60 for a in recent) / len(recent)
    return min(max(avg, 0.5 * cfg.slot_minutes), 2.0 * cfg.slot_minutes)


def compute_etas(
    cfg: ClinicConfig,
    appts: list[Appointment],
    state: SessionState | None,
    window: SessionWindow,
    now: datetime,
) -> dict[uuid.UUID, Eta]:
    """10A.3. Never returns an ETA earlier than the patient's own slot start."""
    avg = timedelta(minutes=avg_consult_minutes(cfg, appts))
    started = state.doctor_started_at if state else None
    cursor = max(window.start_utc, now) if started is None else now
    if state is not None and state.delay_until is not None:
        cursor = max(cursor, state.delay_until)
    current = [a for a in appts if a.queue_status == "in_consultation"]
    ahead = 0
    for c in current:
        elapsed = now - (c.consult_started_at or now)
        cursor += max(avg - elapsed, timedelta(0))
        ahead += 1
    out: dict[uuid.UUID, Eta] = {}
    for a in sorted((a for a in appts if a.queue_status in WAITING), key=order_key):
        start = max(a.start_utc, cursor)
        ws = ceil_to(start, cfg.queue.eta_round_minutes)
        out[a.id] = Eta(start, ws, ws + timedelta(minutes=cfg.queue.eta_range_minutes), ahead)
        cursor = start + avg
        ahead += 1
    return out


def grace_deadline(cfg: ClinicConfig, a: Appointment, state: SessionState | None) -> datetime:
    """Slot start + grace. A declared doctor delay, or a later ETA we told the patient, moves the base:
    the doctor's delay never counts as the patient being late (rule 11)."""
    base = a.start_utc
    if state is not None and state.delay_until is not None:
        base = max(base, state.delay_until)
    if a.last_eta_sent_local:
        base = max(base, a.last_eta_sent_local)
    return base + timedelta(minutes=cfg.queue.grace_minutes)


def grace_base(cfg: ClinicConfig, a: Appointment, state: SessionState | None) -> datetime:
    return grace_deadline(cfg, a, state) - timedelta(minutes=cfg.queue.grace_minutes)


# ---------------------------------------------------------------- data access


async def load_session(db: AsyncSession, d: date, session: str) -> list[Appointment]:
    return list(
        (
            await db.scalars(
                select(Appointment)
                .where(Appointment.session_date == d, Appointment.session == session)
                .order_by(Appointment.queue_position)
            )
        ).all()
    )


async def get_state(db: AsyncSession, d: date, session: str) -> SessionState:
    st = await db.scalar(select(SessionState).where(SessionState.session_date == d, SessionState.session == session))
    if st is None:
        st = SessionState(session_date=d, session=session)
        db.add(st)
        await db.flush()
    return st


def _window(d: date, session: str) -> SessionWindow:
    w = session_window(get_clinic(), d, session)
    if w is None:
        raise QueueError("no such session")
    return w


async def session_etas(db: AsyncSession, d: date, session: str) -> tuple[list[Appointment], dict[uuid.UUID, Eta]]:
    appts = await load_session(db, d, session)
    state = await get_state(db, d, session)
    return appts, compute_etas(get_clinic(), appts, state, _window(d, session), clock.now())


async def eta_for(db: AsyncSession, appt: Appointment) -> Eta:
    _, etas = await session_etas(db, appt.session_date, appt.session)
    if appt.id in etas:
        return etas[appt.id]
    cfg = get_clinic()
    ws = ceil_to(appt.start_utc, cfg.queue.eta_round_minutes)
    return Eta(appt.start_utc, ws, ws + timedelta(minutes=cfg.queue.eta_range_minutes), 0)


# ------------------------------------------------------------------- notices


async def send_queue_notice(db: AsyncSession, appt: Appointment, kind: str, template: str, **data) -> bool:
    log = await claim_reminder(db, kind, appointment_id=appt.id)
    if log is None:
        return False
    await outbox.notify(db, appt.patient_id, template, appointment_id=appt.id, reminder_log_id=log.id, **data)
    return True


async def evaluate_notifications(db: AsyncSession, d: date, session: str) -> None:
    """10A.5: 'turn soon' and 'running late' notices. Never announces an earlier ETA."""
    cfg = get_clinic()
    now = clock.now()
    appts, etas = await session_etas(db, d, session)
    state = await get_state(db, d, session)
    window = _window(d, session)
    if state.closed_at or state.cancelled_by_clinic:
        return
    session_live = state.doctor_started_at is not None or now >= window.start_utc - timedelta(minutes=30)
    for a in appts:
        if a.id not in etas or a.is_walk_in or a.status != "booked":
            continue
        eta = etas[a.id]
        if session_live and eta.ahead <= cfg.queue.notify_when_patients_ahead:
            await send_queue_notice(
                db, a, "turn_soon", "queue_turn_soon", ahead=eta.ahead, eta=eta_text(eta.window_start, eta.window_end)
            )
        if a.queue_status == "checked_in":
            continue
        told = a.last_eta_sent_local or a.start_utc
        if (eta.start - told) >= timedelta(minutes=cfg.queue.notify_eta_shift_minutes):
            recent = await db.scalar(
                select(func.max(ReminderLog.created_at)).where(
                    ReminderLog.appointment_id == a.id, ReminderLog.kind.like("delay_%")
                )
            )
            if recent and now - recent < timedelta(minutes=30):
                continue
            n = (
                await db.scalar(
                    select(func.count()).where(ReminderLog.appointment_id == a.id, ReminderLog.kind.like("delay_%"))
                )
            ) + 1
            if await send_queue_notice(
                db, a, f"delay_{n}", "queue_delay_notice", eta=eta_text(eta.window_start, eta.window_end)
            ):
                a.last_eta_sent_local = eta.start


# ------------------------------------------------------------------- mutations


async def check_in(appointment_id: uuid.UUID, actor: str) -> ActionResult:
    async with get_session() as db:
        a = await db.get(Appointment, appointment_id)
        if a is None:
            raise QueueError("not found")
        async with hold(db, session_key(a.session_date, a.session)):
            await db.refresh(a)
            res = await _check_in_locked(db, a, actor)
            await evaluate_notifications(db, a.session_date, a.session)
            await db.commit()
    outbox.kick()
    return res


async def _check_in_locked(db: AsyncSession, a: Appointment, actor: str) -> ActionResult:
    cfg = get_clinic()
    now = clock.now()
    if a.status != "booked":
        raise QueueError("not booked")
    state = await get_state(db, a.session_date, a.session)
    past_grace = now > grace_deadline(cfg, a, state)
    if a.queue_status == "skipped" or (a.queue_status == "running_late" and past_grace):
        lateness = (now - grace_base(cfg, a, state)).total_seconds() / 60
        return await _reinsert_late(db, a, lateness, actor)
    if a.queue_status not in ("scheduled", "running_late"):
        return ActionResult(a, changed=False)
    a.queue_status = "checked_in"
    a.checked_in_at = now
    await qevent(db, a, "checked_in", actor)
    await audit(db, actor, "check_in", "appointment", a.id)
    return ActionResult(a)


async def _reinsert_late(db: AsyncSession, a: Appointment, lateness_min: float, actor: str) -> ActionResult:
    """Rules 5 and 6: re-insert after N waiting patients, respecting the displacement cap."""
    cfg = get_clinic()
    now = clock.now()
    appts = await load_session(db, a.session_date, a.session)
    others = sorted((x for x in appts if x.id != a.id and x.queue_status in LIVE), key=order_key)
    waiting_ci = [x for x in others if x.queue_status == "checked_in"]
    a.queue_status = "checked_in"
    a.checked_in_at = now
    note = None
    if lateness_min <= cfg.queue.max_late_minutes:
        n = cfg.queue.late_reinsert_after
        anchor_idx = min(n, len(waiting_ci)) - 1
        cap = cfg.queue.max_displacements_per_patient
        for j in range(len(waiting_ci) - 1, anchor_idx, -1):
            if waiting_ci[j].displacement_count >= cap:
                anchor_idx = j
                break
        for x in waiting_ci[anchor_idx + 1 :]:
            x.displacement_count += 1
        if anchor_idx >= 0:
            anchor = waiting_ci[anchor_idx]
        else:
            current = [x for x in others if x.queue_status == "in_consultation"]
            anchor = current[0] if current else None
        a.queue_position = _position_after(others, anchor)
        await qevent(db, a, "reinserted", actor, lateness=round(lateness_min), after=anchor.token_label if anchor else None)
    else:
        a.queue_position = (max((x.queue_position for x in others), default=0.0)) + 1.0
        await qevent(db, a, "moved_to_end", actor, lateness=round(lateness_min))
        await db.flush()
        _, etas = await session_etas(db, a.session_date, a.session)
        window = _window(a.session_date, a.session)
        eta = etas.get(a.id)
        avg = timedelta(minutes=avg_consult_minutes(cfg, appts))
        if eta and eta.start + avg > window.end_utc + timedelta(minutes=cfg.queue.overrun_allowance_minutes):
            note = "late_overrun"
            a.needs_staff_decision = note
    a.priority = False
    await audit(db, actor, "check_in_late", "appointment", a.id, lateness=round(lateness_min))
    return ActionResult(a, note=note)


def _position_after(ordered: list[Appointment], anchor: Appointment | None) -> float:
    """Numeric midpoint between anchor and the next live patient in queue order."""
    if anchor is None:
        first = next((x for x in ordered if x.queue_status in WAITING), None)
        return (first.queue_position - 1.0) if first else 1.0
    idx = ordered.index(anchor)
    nxt = next((x for x in ordered[idx + 1 :] if x.queue_status in WAITING), None)
    if nxt is None or nxt.priority != anchor.priority:
        return anchor.queue_position + 1.0
    return (anchor.queue_position + nxt.queue_position) / 2.0


async def call_next(d: date, session: str, actor: str, expected_current: str = ANY) -> ActionResult:
    """Rules 2-4 and 8. `expected_current` makes double taps idempotent: pass the id of the patient
    the board showed as 'with the doctor' (NOBODY for nobody); if it no longer matches, nothing happens."""
    cfg = get_clinic()
    async with get_session() as db:
        async with hold(db, session_key(d, session)):
            now = clock.now()
            state = await get_state(db, d, session)
            appts = await load_session(db, d, session)
            current = next((a for a in appts if a.queue_status == "in_consultation"), None)
            if expected_current != ANY and (str(current.id) if current else NOBODY) != expected_current:
                return ActionResult(current, changed=False)
            if current:
                _finish(current, now)
                await qevent(db, current, "done", actor)
            if state.doctor_started_at is None:
                state.doctor_started_at = now
            state.paused = False
            called = None
            someone_absent_in_grace = False
            for a in sorted((a for a in appts if a.queue_status in WAITING), key=order_key):
                if a.queue_status == "checked_in":
                    early = now < a.start_utc and not a.is_walk_in and not a.priority
                    if early and someone_absent_in_grace:
                        continue  # rule 8: early arrivals don't jump an absent-but-within-grace patient
                    called = a
                    break
                if now > grace_deadline(cfg, a, state):
                    a.queue_status = "skipped"
                    a.skipped_at = now
                    await qevent(db, a, "skipped", actor)
                    await send_queue_notice(db, a, "missed", "queue_missed_turn")
                else:
                    someone_absent_in_grace = True
            if called:
                called.queue_status = "in_consultation"
                called.called_at = now
                called.consult_started_at = now
                await qevent(db, called, "called", actor)
            await audit(db, actor, "call_next", "session", f"{d}:{session}", called=str(called.id) if called else None)
            await db.flush()
            await evaluate_notifications(db, d, session)
            await db.commit()
    outbox.kick()
    return ActionResult(called)


def _finish(a: Appointment, now: datetime) -> None:
    a.queue_status = "done"
    a.consult_ended_at = now
    a.status = "completed"


async def mark_done(appointment_id: uuid.UUID, actor: str) -> ActionResult:
    async with get_session() as db:
        a = await db.get(Appointment, appointment_id)
        async with hold(db, session_key(a.session_date, a.session)):
            await db.refresh(a)
            if a.queue_status != "in_consultation":
                if a.status == "booked" and a.queue_status in WAITING:
                    # reception marks completed without a formal call (e.g. seen elsewhere)
                    a.consult_started_at = a.consult_started_at or clock.now()
                else:
                    return ActionResult(a, changed=False)
            _finish(a, clock.now())
            await qevent(db, a, "done", actor)
            await audit(db, actor, "done", "appointment", a.id)
            await evaluate_notifications(db, a.session_date, a.session)
            await db.commit()
    outbox.kick()
    return ActionResult(a)


async def set_priority(appointment_id: uuid.UUID, actor: str, value: bool = True) -> ActionResult:
    async with get_session() as db:
        a = await db.get(Appointment, appointment_id)
        async with hold(db, session_key(a.session_date, a.session)):
            await db.refresh(a)
            a.priority = value
            await qevent(db, a, "priority_set", actor, value=value)
            await audit(db, actor, "priority_set", "appointment", a.id, value=value)
            await db.flush()
            await evaluate_notifications(db, a.session_date, a.session)
            await db.commit()
    outbox.kick()
    return ActionResult(a)


async def set_delay(d: date, session: str, minutes: int, actor: str, paused: bool | None = None) -> None:
    async with get_session() as db:
        async with hold(db, session_key(d, session)):
            st = await get_state(db, d, session)
            now = clock.now()
            minutes = max(0, int(minutes))
            window = _window(d, session)
            base = window.start_utc if (st.doctor_started_at is None and now < window.start_utc) else now
            if minutes:
                st.delay_until = base + timedelta(minutes=minutes)
            elif st.delay_until is not None:
                st.delay_until = min(st.delay_until, now)  # "resume": doctor available from now
            st.doctor_delay_minutes = minutes
            if paused is not None:
                st.paused = paused
            await audit(db, actor, "doctor_delay", "session", f"{d}:{session}", minutes=minutes, paused=paused)
            await db.flush()
            await evaluate_notifications(db, d, session)
            await db.commit()
    outbox.kick()


async def close_session(d: date, session: str, actor: str) -> int:
    """Rule 10. Returns how many patients were marked no-show. Idempotent."""
    now = clock.now()
    marked = 0
    async with get_session() as db:
        async with hold(db, session_key(d, session)):
            st = await get_state(db, d, session)
            appts = await load_session(db, d, session)
            for a in appts:
                if a.status != "booked":
                    continue
                if a.queue_status == "in_consultation":
                    _finish(a, now)
                    await qevent(db, a, "done", actor)
                elif a.queue_status in ("scheduled", "running_late", "skipped"):
                    a.queue_status = "no_show"
                    a.status = "no_show"
                    patient = await db.get(Patient, a.patient_id)
                    patient.noshow_count += 1
                    await qevent(db, a, "no_show", actor)
                    await send_queue_notice(db, a, "no_show", "appt_no_show")
                    marked += 1
            if st.closed_at is None:
                st.closed_at = now
            await audit(db, actor, "close_session", "session", f"{d}:{session}", no_shows=marked)
            await db.commit()
    outbox.kick()
    return marked


async def add_walk_in(d: date, session: str, actor: str, name: str = "", phone: str | None = None) -> Appointment:
    cfg = get_clinic()
    now = clock.now()
    async with get_session() as db:
        async with hold(db, session_key(d, session)):
            if phone:
                patient, _ = await get_or_create_patient(db, phone)
            else:
                patient = Patient(phone_hash=phone_hash(f"+0walkin{uuid.uuid4().hex}"), phone_enc=None, opted_out=True)
                db.add(patient)
                await db.flush()
            if name:
                patient.name_enc = encrypt(name.strip()[:80])
            appts = await load_session(db, d, session)
            n = sum(1 for a in appts if a.is_walk_in) + 1
            ordered = sorted((a for a in appts if a.queue_status in LIVE), key=order_key)
            # after everyone whose time has come or who is already waiting; never ahead of an on-time booking
            due = [a for a in ordered if a.queue_status in ("checked_in", "in_consultation") or a.start_utc <= now]
            anchor = due[-1] if due else None
            appt = Appointment(
                patient_id=patient.id,
                start_utc=now,
                end_utc=now + timedelta(minutes=cfg.slot_minutes),
                status="booked",
                source="admin",
                session_date=d,
                session=session,
                token_no=n,
                token_label=tokens.walk_in_label(cfg, n),
                is_walk_in=True,
                queue_status="checked_in",
                checked_in_at=now,
                queue_position=_position_after(ordered, anchor),
            )
            db.add(appt)
            await db.flush()
            await qevent(db, appt, "checked_in", actor, walk_in=True)
            await audit(db, actor, "walk_in", "appointment", appt.id)
            await db.commit()
    return appt


async def report_running_late(appointment_id: uuid.UUID, minutes: int, actor: str = "patient") -> Appointment:
    async with get_session() as db:
        a = await db.get(Appointment, appointment_id)
        async with hold(db, session_key(a.session_date, a.session)):
            await db.refresh(a)
            if a.status == "booked" and a.queue_status in ("scheduled", "running_late"):
                a.queue_status = "running_late"
                a.patient_reported_late_minutes = int(minutes)
                await qevent(db, a, "marked_late", actor, minutes=int(minutes))
                outbox.notify_staff(db, "handoff.staff_alert", who=a.token_label, reason=f"running late ~{int(minutes)} min")
            await db.commit()
    outbox.kick()
    return a


async def mark_arrived_hint(appointment_id: uuid.UUID) -> None:
    async with get_session() as db:
        a = await db.get(Appointment, appointment_id)
        if a:
            a.arrived_hint_at = clock.now()
            await db.commit()


async def todays_appointment(db: AsyncSession, patient_id) -> Appointment | None:
    d = local_today(clock.now())
    return await db.scalar(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.session_date == d,
            Appointment.status.in_(("booked", "completed")),
        )
        .order_by(Appointment.start_utc)
    )


async def queue_status_for(db: AsyncSession, patient_id) -> dict | None:
    """Data for get_my_queue_status: token, status, ahead, now serving, ETA window."""
    a = await db.scalar(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.session_date == local_today(clock.now()),
            Appointment.status == "booked",
        )
        .order_by(Appointment.start_utc)
    )
    if a is None:
        return None
    appts, etas = await session_etas(db, a.session_date, a.session)
    current = next((x for x in appts if x.queue_status == "in_consultation"), None)
    eta = etas.get(a.id)
    return {
        "appointment": a,
        "token": a.token_label,
        "queue_status": a.queue_status,
        "ahead": eta.ahead if eta else 0,
        "current": current.token_label if current else None,
        "eta": eta_text(eta.window_start, eta.window_end) if eta else None,
    }
