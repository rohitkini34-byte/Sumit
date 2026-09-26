"""Freed-slot flow: move-earlier offers, cascade, then the waitlist (section 10B.4)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.slots import session_of_slot
from app.config import get_clinic
from app.db.models import Appointment, Patient, SessionState, SlotOffer, Waitlist
from app.db.session import get_session, hold, session_key, slot_key
from app.dev import clock
from app.scheduling import outbox
from app.scheduling.common import DomainError, audit


class OfferUnavailable(DomainError):
    code = "offer.taken"


class WaitlistLimit(DomainError):
    code = "waitlist.limit"


# Hook set by the scheduler so holds expire precisely at expires_at.
expiry_timer_hook = None


async def _slot_free(db: AsyncSession, slot_start: datetime) -> bool:
    taken = await db.scalar(
        select(Appointment.id).where(
            Appointment.start_utc == slot_start, Appointment.status == "booked", Appointment.is_walk_in.is_(False)
        )
    )
    return taken is None


async def _session_open(db: AsyncSession, d: date, session: str) -> bool:
    st = await db.scalar(select(SessionState).where(SessionState.session_date == d, SessionState.session == session))
    return st is None or (not st.cancelled_by_clinic and st.closed_at is None)


def _arm_timer(expires_at: datetime) -> None:
    if expiry_timer_hook:
        expiry_timer_hook(expires_at)


async def start_freed_slot_flow(
    db: AsyncSession, slot_start: datetime, *, source_appointment_id: uuid.UUID | None, depth: int
) -> list[SlotOffer]:
    cfg = get_clinic()
    now = clock.now()
    d, session = session_of_slot(cfg, slot_start)
    oe = cfg.changes.offer_earlier_slot
    if not await _session_open(db, d, session):
        return []
    # 6: too close to the time -> no offers, the queue simply moves up
    if slot_start - now < timedelta(minutes=oe.min_lead_minutes):
        return []
    if not await _slot_free(db, slot_start):
        return []
    if oe.enabled and depth < oe.max_cascade:
        candidates = (
            await db.execute(
                select(Appointment)
                .join(Patient, Patient.id == Appointment.patient_id)
                .where(
                    Appointment.session_date == d,
                    Appointment.session == session,
                    Appointment.status == "booked",
                    Appointment.is_walk_in.is_(False),
                    Appointment.queue_status == "scheduled",
                    Appointment.start_utc > slot_start,
                    Patient.opted_out.is_(False),
                )
                .order_by(Appointment.start_utc)
                .limit(oe.offer_to_count)
            )
        ).scalars().all()
        if candidates:
            made = []
            expires = now + timedelta(minutes=oe.hold_minutes)
            for a in candidates:
                o = SlotOffer(
                    slot_start_utc=slot_start,
                    session_date=d,
                    session=session,
                    source_appointment_id=source_appointment_id,
                    offer_type="move_earlier",
                    offered_to_appointment_id=a.id,
                    patient_id=a.patient_id,
                    status="open",
                    expires_at=expires,
                    cascade_depth=depth,
                )
                db.add(o)
                await db.flush()
                await outbox.notify(db, a.patient_id, "slot_offer_earlier", dedupe_key=f"offer:{o.id}", offer_id=o.id)
                made.append(o)
            _arm_timer(expires)
            return made
    o = await offer_to_waitlist(db, slot_start, source_appointment_id=source_appointment_id, depth=depth)
    return [o] if o else []


async def offer_to_waitlist(
    db: AsyncSession, slot_start: datetime, *, source_appointment_id=None, depth: int = 0
) -> SlotOffer | None:
    cfg = get_clinic()
    wl = cfg.changes.waitlist
    now = clock.now()
    if not wl.enabled:
        return None
    d, session = session_of_slot(cfg, slot_start)
    if not await _session_open(db, d, session) or not await _slot_free(db, slot_start):
        return None
    if slot_start - now < timedelta(minutes=cfg.changes.offer_earlier_slot.min_lead_minutes):
        return None
    already = select(SlotOffer.offered_to_waitlist_id).where(
        SlotOffer.slot_start_utc == slot_start, SlotOffer.offered_to_waitlist_id.is_not(None)
    )
    entry = await db.scalar(
        select(Waitlist)
        .join(Patient, Patient.id == Waitlist.patient_id)
        .where(
            Waitlist.session_date == d,
            Waitlist.session.in_((session, "any")),
            Waitlist.status == "waiting",
            Waitlist.id.not_in(already),
            Patient.opted_out.is_(False),
        )
        .order_by(Waitlist.created_at)
    )
    if entry is None:
        return None  # 5: slot simply stays open for normal booking
    expires = now + timedelta(minutes=wl.hold_minutes)
    entry.status = "offered"
    o = SlotOffer(
        slot_start_utc=slot_start,
        session_date=d,
        session=session,
        source_appointment_id=source_appointment_id,
        offer_type="waitlist",
        offered_to_waitlist_id=entry.id,
        patient_id=entry.patient_id,
        status="open",
        expires_at=expires,
        cascade_depth=depth,
    )
    db.add(o)
    await db.flush()
    await outbox.notify(db, entry.patient_id, "waitlist_offer", dedupe_key=f"offer:{o.id}", offer_id=o.id)
    _arm_timer(expires)
    return o


async def _open_offers_for_slot(db: AsyncSession, slot_start: datetime) -> list[SlotOffer]:
    return list(
        (
            await db.scalars(select(SlotOffer).where(SlotOffer.slot_start_utc == slot_start, SlotOffer.status == "open"))
        ).all()
    )


async def accept_offer(offer_id: uuid.UUID, patient_id: uuid.UUID, actor: str = "patient") -> Appointment:
    from app.scheduling import booking_service, change_service

    cfg = get_clinic()
    async with get_session() as db:
        offer = await db.get(SlotOffer, offer_id)
        if offer is None or offer.patient_id != patient_id:
            raise OfferUnavailable()
        keys = [session_key(offer.session_date, offer.session), slot_key(offer.slot_start_utc)]
        appt = None
        if offer.offered_to_appointment_id:
            appt = await db.get(Appointment, offer.offered_to_appointment_id)
            if appt:
                keys += [session_key(appt.session_date, appt.session), slot_key(appt.start_utc)]
        async with hold(db, *keys):
            await db.refresh(offer)
            if offer.status != "open" or offer.expires_at <= clock.now():
                raise OfferUnavailable()
            if not await _slot_free(db, offer.slot_start_utc):
                offer.status = "superseded"
                await db.commit()
                raise OfferUnavailable()
            if offer.offer_type == "move_earlier":
                await db.refresh(appt)
                if appt.status != "booked" or appt.queue_status != "scheduled":
                    raise OfferUnavailable()
                new = await change_service.reschedule_locked(
                    db, appt, offer.slot_start_utc, by="patient", actor=actor,
                    cascade_depth=offer.cascade_depth + 1, via_offer=True,
                )
            else:
                patient = await db.get(Patient, patient_id)
                new = await booking_service.book_locked(db, patient, offer.slot_start_utc, actor=actor)
                await booking_service.renumber_sequential(db, [(new.session_date, new.session)], actor, new_ids={new.id})
                await outbox.notify(db, patient_id, "appt_confirmation", dedupe_key=f"confirm:{new.id}", appointment_id=new.id)
                entry = await db.get(Waitlist, offer.offered_to_waitlist_id)
                entry.status = "booked"
            offer.status = "accepted"
            for other in await _open_offers_for_slot(db, offer.slot_start_utc):
                if other.id != offer.id:
                    other.status = "superseded"
                    if other.offered_to_waitlist_id:
                        e = await db.get(Waitlist, other.offered_to_waitlist_id)
                        if e and e.status == "offered":
                            e.status = "waiting"
            await audit(db, actor, "offer_accepted", "slot_offer", offer.id, type=offer.offer_type)
            await db.commit()
    outbox.kick()
    return new


async def decline_offer(offer_id: uuid.UUID, patient_id: uuid.UUID, actor: str = "patient") -> None:
    async with get_session() as db:
        offer = await db.get(SlotOffer, offer_id)
        if offer is None or offer.patient_id != patient_id:
            raise OfferUnavailable()
        async with hold(db, session_key(offer.session_date, offer.session), slot_key(offer.slot_start_utc)):
            await db.refresh(offer)
            if offer.status != "open":
                return
            offer.status = "declined"
            if offer.offered_to_waitlist_id:
                e = await db.get(Waitlist, offer.offered_to_waitlist_id)
                if e and e.status == "offered":
                    e.status = "waiting"  # keeps their place for other slots
            await db.flush()
            await _advance_if_unclaimed(db, offer)
            await db.commit()
    outbox.kick()


async def _advance_if_unclaimed(db: AsyncSession, offer: SlotOffer) -> None:
    """When nobody holds a slot any more, move on to the next waitlist entry."""
    if await _open_offers_for_slot(db, offer.slot_start_utc):
        return
    accepted = await db.scalar(
        select(func.count()).where(SlotOffer.slot_start_utc == offer.slot_start_utc, SlotOffer.status == "accepted")
    )
    if accepted:
        return
    await offer_to_waitlist(
        db, offer.slot_start_utc, source_appointment_id=offer.source_appointment_id, depth=offer.cascade_depth
    )


async def expire_offers() -> int:
    """Run by the 5-minute job and by precise timers at expires_at."""
    now = clock.now()
    n = 0
    async with get_session() as db:
        due = (
            await db.scalars(select(SlotOffer).where(SlotOffer.status == "open", SlotOffer.expires_at <= now))
        ).all()
        slots = {}
        for o in due:
            slots.setdefault(o.slot_start_utc, o)
    for slot_start, sample in slots.items():
        async with get_session() as db:
            async with hold(db, session_key(sample.session_date, sample.session), slot_key(slot_start)):
                expired = (
                    await db.scalars(
                        select(SlotOffer).where(
                            SlotOffer.slot_start_utc == slot_start,
                            SlotOffer.status == "open",
                            SlotOffer.expires_at <= clock.now(),
                        )
                    )
                ).all()
                last = None
                for o in expired:
                    o.status = "expired"
                    n += 1
                    last = o
                    if o.offered_to_waitlist_id:
                        e = await db.get(Waitlist, o.offered_to_waitlist_id)
                        if e and e.status == "offered":
                            e.status = "expired"
                if last is not None:
                    await db.flush()
                    await _advance_if_unclaimed(db, last)
                await db.commit()
    if n:
        outbox.kick()
    return n


async def join_waitlist(patient_id: uuid.UUID, d: date, session: str = "any") -> int:
    """Returns the patient's position (1-based) in that day's waitlist."""
    cfg = get_clinic()
    async with get_session() as db:
        mine = await db.scalar(
            select(func.count()).where(
                Waitlist.patient_id == patient_id, Waitlist.status.in_(("waiting", "offered"))
            )
        )
        existing = await db.scalar(
            select(Waitlist).where(
                Waitlist.patient_id == patient_id,
                Waitlist.session_date == d,
                Waitlist.status.in_(("waiting", "offered")),
            )
        )
        if existing is None:
            if mine >= cfg.changes.waitlist.max_per_patient:
                raise WaitlistLimit(max=cfg.changes.waitlist.max_per_patient)
            existing = Waitlist(patient_id=patient_id, session_date=d, session=session, created_at=clock.now())
            db.add(existing)
            await db.flush()
            await audit(db, "patient", "waitlist_join", "waitlist", existing.id)
        pos = await db.scalar(
            select(func.count()).where(
                Waitlist.session_date == d,
                Waitlist.status.in_(("waiting", "offered")),
                Waitlist.created_at <= existing.created_at,
            )
        )
        await db.commit()
        return int(pos)
