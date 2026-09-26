"""Outbox delivery: turns queued notifications into WhatsApp messages / calendar calls.

Messages are rendered at send time from the *current* DB state (current slot, token, ETA),
so a reminder queued before a reschedule never shows stale details.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.base import CalendarError, get_calendar
from app.config import get_clinic, get_settings
from app.db.models import Appointment, FollowUp, Patient, ReminderLog, SlotOffer
from app.dev import clock
from app.i18n import t
from app.scheduling.common import calendar_summary, first_name, patient_name, recipient, short_ref
from app.timeutil import fmt_date, fmt_short, fmt_time, minutes_between
from app.whatsapp.client import OutsideWindow, Recipient, SendBlocked, get_wa

log = logging.getLogger(__name__)


def _uuid(v) -> uuid.UUID | None:
    return uuid.UUID(v) if v else None


def eta_text(start: datetime, end: datetime) -> str:
    return f"{fmt_time(start)}–{fmt_time(end)}"


async def claim_reminder(
    db: AsyncSession, kind: str, *, appointment_id=None, follow_up_id=None
) -> ReminderLog | None:
    """Insert the reminder_log row first; the unique constraint makes each (entity, kind) send once."""
    row = ReminderLog(appointment_id=appointment_id, follow_up_id=follow_up_id, kind=kind, status="pending")
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        return None
    return row


async def _mark_log(db: AsyncSession, log_id, status: str, wa_id: str | None = None) -> None:
    if not log_id:
        return
    row = await db.get(ReminderLog, _uuid(log_id))
    if row:
        row.status = status
        row.wa_message_id = wa_id
        if status == "sent":
            row.sent_at = clock.now()


async def deliver_whatsapp(db: AsyncSession, p: dict) -> None:
    template = p["template"]
    if template == "staff":
        await _deliver_staff(p)
        return
    patient = await db.get(Patient, _uuid(p["patient_id"]))
    if patient is None or patient.anonymised_at is not None:
        return
    r = recipient(patient)
    try:
        built = await _build(db, patient, r, p)
        if built is None:  # nothing to send any more (e.g. offer already closed)
            await _mark_log(db, p.get("reminder_log_id"), "skipped")
            return
        name, params, buttons, extra = built
        if name == "text":
            wa_id = await get_wa().send_text(r, params[0])
        else:
            wa_id = await get_wa().notify(r, name, params, buttons, extra_text=extra)
        await _mark_log(db, p.get("reminder_log_id"), "sent", wa_id)
    except OutsideWindow:
        await _mark_log(db, p.get("reminder_log_id"), "skipped")
    except SendBlocked as exc:
        log.info("notification blocked: %s", exc)
        await _mark_log(db, p.get("reminder_log_id"), "blocked")


async def _deliver_staff(p: dict) -> None:
    from app.whatsapp.client import SendFailed

    vars_ = {k: v for k, v in p.items() if k not in ("template", "key")}
    text = t(p["key"], "en", **vars_)
    for phone in get_settings().staff_numbers:
        try:
            await get_wa().send_text(Recipient(phone=phone, is_staff=True), text)
        except (SendBlocked, SendFailed) as exc:
            log.warning("staff alert not delivered: %s", type(exc).__name__)


def _report_by(start: datetime) -> str:
    return fmt_time(start - timedelta(minutes=get_clinic().queue.report_before_minutes))


async def _build(db: AsyncSession, patient: Patient, r: Recipient, p: dict):
    """Returns (template_name, params, button_payloads, extra_text) or None."""
    cfg = get_clinic()
    lang = r.lang
    name = first_name(patient) or patient_name(patient) or "—"
    template = p["template"]

    if template == "text":
        return "text", [t(p["key"], lang, **p.get("vars", {}))], [], ""

    if template == "followup_reminder":
        fu = await db.get(FollowUp, _uuid(p["follow_up_id"]))
        if fu is None or fu.status in ("booked", "cancelled"):
            return None
        return template, [name, cfg.doctor_name, fmt_date(fu.due_date)], [f"fu_book:{fu.id}", f"fu_no:{fu.id}"], ""

    if template in ("slot_offer_earlier", "waitlist_offer"):
        offer = await db.get(SlotOffer, _uuid(p["offer_id"]))
        if offer is None or offer.status != "open":
            return None
        minutes = max(1, int(minutes_between(clock.now(), offer.expires_at)))
        if template == "slot_offer_earlier":
            appt = await db.get(Appointment, offer.offered_to_appointment_id)
            if appt is None or appt.status != "booked":
                return None
            current = f"{appt.token_label} {fmt_short(appt.start_utc)}"
            return template, [name, current, fmt_short(offer.slot_start_utc), str(minutes)], [f"offer_yes:{offer.id}", f"offer_no:{offer.id}"], ""
        return template, [name, fmt_date(offer.slot_start_utc), fmt_time(offer.slot_start_utc), str(minutes)], [f"wl_yes:{offer.id}", f"wl_no:{offer.id}"], ""

    appt = await db.get(Appointment, _uuid(p["appointment_id"]))
    if appt is None:
        return None
    start = appt.start_utc

    if template == "appt_confirmation":
        if appt.status != "booked":
            return None
        rule = t("book.rule_line", lang, grace=cfg.queue.grace_minutes, reinsert=cfg.queue.late_reinsert_after)
        return template, [name, fmt_date(start), appt.token_label, fmt_time(start), _report_by(start), cfg.doctor_name], [], rule

    if template == "appt_reminder_24h":
        if appt.status != "booked":
            return None
        extra = ""
        if patient.noshow_count >= cfg.queue.noshow_warn_threshold:
            extra = t("tpl.appt_reminder_24h.confirm_note", lang)
        return template, [name, f"{fmt_date(start)}", appt.token_label, fmt_time(start)], [f"r24_confirm:{appt.id}", f"r24_resched:{appt.id}", f"r24_cancel:{appt.id}"], extra

    if template == "appt_reminder_2h":
        if appt.status != "booked":
            return None
        from app.scheduling import queue_service

        eta = await queue_service.eta_for(db, appt)
        appt.last_eta_sent_local = max(eta.start, appt.last_eta_sent_local or eta.start)
        return template, [name, appt.token_label, eta_text(eta.start, eta.end), cfg.address], [f"r2_onway:{appt.id}", f"r2_late:{appt.id}", f"r2_cancel:{appt.id}"], ""

    if template == "queue_turn_soon":
        if appt.status != "booked":
            return None
        return template, [appt.token_label, str(p["ahead"]), p["eta"]], [], ""

    if template == "queue_delay_notice":
        if appt.status != "booked":
            return None
        return template, [appt.token_label, p["eta"]], [f"delay_ok:{appt.id}", f"delay_cancel:{appt.id}"], ""

    if template == "queue_missed_turn":
        if appt.queue_status != "skipped":
            return None
        return template, [appt.token_label, t("tpl.queue_missed_turn.line", lang)], [f"missed_coming:{appt.id}", f"missed_resched:{appt.id}"], ""

    if template == "appt_no_show":
        return template, [name, fmt_date(start)], [f"book_again:{appt.id}"], ""

    if template == "appt_cancelled":
        return template, [name, f"{fmt_date(start)} {fmt_time(start)}", appt.token_label], [f"book_again:{appt.id}"], ""

    if template == "appt_rescheduled":
        if appt.status != "booked":
            return None
        return template, [name, f"{fmt_date(start)}", appt.token_label, fmt_time(start), _report_by(start)], [], ""

    if template == "token_updated":
        if appt.status != "booked" or appt.token_label == p.get("old_token"):
            return None
        return template, [name, p["old_token"], appt.token_label, fmt_time(start)], [], ""

    if template == "session_cancelled_by_clinic":
        return template, [name, fmt_date(start), t(f"session.{appt.session}", lang)], [f"sc_resched:{appt.id}", f"sc_cancel:{appt.id}"], ""

    raise ValueError(f"unknown notification {template}")


async def deliver_gcal(db: AsyncSession, p: dict) -> None:
    cal = get_calendar()
    op = p["op"]
    if op == "delete":
        await cal.delete(p["event_id"])
        return
    appt = await db.get(Appointment, _uuid(p["appointment_id"]))
    if appt is None:
        return
    if op == "patch":
        if appt.status != "booked":
            return
        try:
            await cal.patch(p["event_id"], appt.start_utc, appt.end_utc)
            return
        except CalendarError:
            op = "insert"  # event gone: fall back to delete + insert
            await cal.delete(p["event_id"])
    if op == "insert":
        if appt.status != "booked":
            return
        patient = await db.get(Patient, appt.patient_id)
        event_id = await cal.insert(
            str(appt.id),
            calendar_summary(patient_name(patient)),
            f"Booked via WhatsApp. Ref: {short_ref(appt.id)}",
            appt.start_utc,
            appt.end_utc,
        )
        appt.gcal_event_id = event_id
        return
    raise ValueError(f"unknown gcal op {op}")


async def today_sessions(db: AsyncSession) -> list[tuple[date, str]]:
    from app.calendar.slots import session_windows
    from app.timeutil import local_today

    d = local_today(clock.now())
    return [(d, w.name) for w in session_windows(get_clinic(), d, include_holidays=True)]
