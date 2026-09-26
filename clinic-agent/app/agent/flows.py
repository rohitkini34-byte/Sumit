"""Conversation flows shared by the deterministic router and the LLM tools.

Every flow validates ownership and state server-side; nothing trusts ids from the model.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.agent import state as S
from app.agent.state import Ctx
from app.calendar.slots import sign_slot_id, verify_slot_id
from app.config import get_clinic, get_settings
from app.db.crypto import mask_phone
from app.db.models import Appointment, FollowUp
from app.db.session import get_session
from app.dev import clock
from app.i18n import t
from app.scheduling import booking_service, change_service, offers, outbox, queue_service, tokens
from app.scheduling.common import DomainError, first_name, patient_name
from app.timeutil import fmt_date, fmt_short, fmt_time, local_today
from app.whatsapp.client import SendBlocked, get_wa

log = logging.getLogger(__name__)
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ------------------------------------------------------------------ sending


async def say(ctx: Ctx, key: str, **kw) -> str:
    text = t(key, ctx.lang, **kw)
    await send_text(ctx, text)
    return text


async def send_text(ctx: Ctx, text: str, *, allow_opted_out: bool = False) -> None:
    try:
        await get_wa().send_text(ctx.r, text, allow_opted_out=allow_opted_out)
        ctx.sent.append(text)
    except SendBlocked as exc:
        log.info("reply blocked: %s", exc)


async def send_buttons(ctx: Ctx, body: str, buttons: list[tuple[str, str]]) -> None:
    try:
        await get_wa().send_buttons(ctx.r, body, buttons)
        ctx.sent.append(body)
    except SendBlocked as exc:
        log.info("reply blocked: %s", exc)


async def send_list(ctx: Ctx, body: str, button: str, rows: list[tuple[str, str, str]], section: str) -> None:
    try:
        await get_wa().send_list(ctx.r, body, button, rows, section)
        ctx.sent.append(body)
    except SendBlocked as exc:
        log.info("reply blocked: %s", exc)


def who(ctx: Ctx) -> str:
    from app.scheduling.common import patient_phone

    name = first_name(ctx.patient)
    return f"{name} ({mask_phone(patient_phone(ctx.patient))})" if name else mask_phone(patient_phone(ctx.patient))


async def send_menu(ctx: Ctx) -> None:
    lang = ctx.lang
    await send_buttons(
        ctx,
        t("menu.body", lang),
        [("menu:book", t("menu.book", lang)), ("menu:my", t("menu.my", lang)), ("menu:info", t("menu.info", lang))],
    )


# -------------------------------------------------------------------- slots


def slot_id_for(start: datetime) -> str:
    return sign_slot_id(start, get_settings().SESSION_SECRET)


def slot_from_id(slot_id: str) -> datetime | None:
    return verify_slot_id(slot_id, get_settings().SESSION_SECRET)


async def find_and_send_slots(
    ctx: Ctx,
    date_from: date,
    date_to: date,
    part_of_day: str | None = None,
    not_before: time | None = None,
    *,
    intro_key: str = "slots.list_body",
) -> list[dict]:
    cfg = get_clinic()
    today = local_today(clock.now())
    date_from = max(date_from, today)
    date_to = min(max(date_to, date_from), today + timedelta(days=cfg.booking_horizon_days))
    async with get_session() as db:
        found = await booking_service.find_slots(db, date_from, date_to, part_of_day, not_before, limit=10)
    items = []
    for s in found:
        _, _, _, label = tokens.slot_token(cfg, s)
        items.append({"slot_id": slot_id_for(s), "start_local_iso": s.astimezone(cfg_tz()).isoformat(),
                      "label": fmt_short(s), "token": label})
    if not items:
        return []
    ctx.state = S.RESCHEDULE_CHOOSING_SLOT if ctx.context.get("resched_appt_id") else S.CHOOSING_SLOT
    lang = ctx.lang
    await send_list(
        ctx,
        t(intro_key, lang),
        t("slots.list_button", lang),
        [(f"slot:{i['slot_id']}", i["label"], f"Token {i['token']}") for i in items],
        t("slots.section", lang),
    )
    ctx.context["last_search"] = {"from": date_from.isoformat(), "to": date_to.isoformat(), "part": part_of_day}
    return items


def cfg_tz():
    return get_settings().tz


async def resend_last_search(ctx: Ctx, intro_key: str = "slots.retry") -> None:
    ls = ctx.context.get("last_search") or {}
    today = local_today(clock.now())
    d_from = date.fromisoformat(ls["from"]) if ls.get("from") else today
    d_to = date.fromisoformat(ls["to"]) if ls.get("to") else today + timedelta(days=3)
    items = await find_and_send_slots(ctx, d_from, d_to, ls.get("part"), intro_key=intro_key)
    if not items:
        items = await find_and_send_slots(ctx, today, today + timedelta(days=7), intro_key=intro_key)
    if not items:
        await say(ctx, "slots.none")


# ------------------------------------------------------------------ booking


async def slot_chosen(ctx: Ctx, slot_id: str) -> None:
    """Deterministic handler for a tapped slot row."""
    start = slot_from_id(slot_id)
    if start is None:
        await say(ctx, "stale")
        return
    if ctx.context.get("resched_appt_id"):
        await begin_reschedule_confirm(ctx, start)
        return
    name = patient_name(ctx.patient)
    if not name:
        ctx.context["pending_slot"] = start.isoformat()
        ctx.state = S.ASKING_NAME
        await say(ctx, "ask_name")
        return
    await begin_booking_confirm(ctx, start, name)


async def name_received(ctx: Ctx, text: str) -> bool:
    name = " ".join(text.split())[:60]
    if len(name) < 2 or any(ch.isdigit() for ch in name):
        await say(ctx, "ask_name")
        return False
    pending = ctx.context.pop("pending_slot", None)
    if not pending:
        ctx.state = S.IDLE
        return False
    await begin_booking_confirm(ctx, datetime.fromisoformat(pending), name)
    return True


async def begin_booking_confirm(
    ctx: Ctx, start: datetime, name: str, age: int | None = None, reason: str | None = None
) -> dict:
    cfg = get_clinic()
    draft = ctx.new_draft("book", start, name=name[:60], age=age, reason=(reason or "")[:60] or None)
    _, _, _, label = tokens.slot_token(cfg, start)
    lang = ctx.lang
    ctx.state = S.CONFIRMING_BOOKING
    await send_buttons(
        ctx,
        t("book.confirm_body", lang, date=fmt_date(start), time=fmt_time(start), token=label, name=name),
        [(f"confirm_book:{draft['id']}", t("book.confirm_btn", lang)), (f"change_slot:{draft['id']}", t("book.change_btn", lang))],
    )
    return draft


async def confirm_booking(ctx: Ctx, draft_id: str) -> None:
    draft, expired = ctx.get_draft(draft_id)
    if draft is None or draft.get("kind") != "book":
        await say(ctx, "stale")
        return
    if expired:
        ctx.clear_draft()
        await say(ctx, "book.expired")
        await resend_last_search(ctx, intro_key="slots.list_body")
        return
    start = datetime.fromisoformat(draft["start"])
    try:
        appt = await booking_service.book(
            ctx.patient.id, start, name=draft.get("name"), age=draft.get("age"), reason=draft.get("reason")
        )
    except (booking_service.SlotTaken, booking_service.InvalidSlot):
        ctx.clear_draft()
        await resend_last_search(ctx)  # graceful retry with fresh slots
        return
    except booking_service.BookingLimit as exc:
        ctx.clear_draft()
        ctx.state = S.IDLE
        await say(ctx, "book.limit", max=exc.ctx["max"], phone=get_clinic().phone)
        return
    except DomainError:
        ctx.clear_draft()
        ctx.state = S.IDLE
        await say(ctx, "book.failed", phone=get_clinic().phone)
        return
    ctx.clear_draft()
    ctx.state = S.IDLE
    fu_id = ctx.context.pop("followup_id", None)
    if fu_id:
        async with get_session() as db:
            fu = await db.get(FollowUp, uuid.UUID(fu_id))
            if fu and fu.patient_id == ctx.patient.id:
                fu.status = "booked"
                fu.appointment_id = appt.id
                await db.commit()
    await ctx.refresh_patient()
    await outbox.process_outbox()  # send the confirmation right away


# --------------------------------------------------------------- appointments


async def own_appointment(ctx: Ctx, appt_id: str) -> Appointment | None:
    try:
        aid = uuid.UUID(str(appt_id))
    except ValueError:
        return None
    async with get_session() as db:
        a = await db.get(Appointment, aid)
    if a is None or a.patient_id != ctx.patient.id:
        return None
    return a


async def upcoming(ctx: Ctx) -> list[Appointment]:
    async with get_session() as db:
        return await booking_service.active_bookings(db, ctx.patient.id)


async def list_appointments(ctx: Ctx) -> list[dict]:
    out = []
    for a in await upcoming(ctx):
        async with get_session() as db:
            eta = await queue_service.eta_for(db, a)
        out.append({
            "appointment_id": str(a.id),
            "token": a.token_label,
            "date": fmt_date(a.start_utc),
            "slot_time": fmt_time(a.start_utc),
            "expected_time_approx": f"{fmt_time(eta.window_start)}–{fmt_time(eta.window_end)}",
            "queue_status": a.queue_status,
        })
    return out


async def say_appointments(ctx: Ctx) -> None:
    items = await list_appointments(ctx)
    if not items:
        await say(ctx, "my.none")
        return
    lines = [t("my.header", ctx.lang)] + [
        t("my.item", ctx.lang, token=i["token"], date=i["date"], time=i["expected_time_approx"]) for i in items
    ]
    await send_text(ctx, "\n".join(lines))


async def ask_cancel(ctx: Ctx, appt_id: str) -> str:
    a = await own_appointment(ctx, appt_id)
    if a is None or a.status != "booked":
        await say(ctx, "stale")
        return "not_found"
    if a.queue_status in change_service.BLOCKED_FOR_PATIENT:
        await say(ctx, "cancel.not_allowed")
        return "not_allowed"
    lang = ctx.lang
    ctx.state = S.CONFIRMING_CANCEL
    await send_buttons(
        ctx,
        t("cancel.confirm_body", lang, date=fmt_date(a.start_utc), time=fmt_time(a.start_utc), token=a.token_label),
        [(f"cancel_yes:{a.id}", t("cancel.yes", lang)), (f"cancel_no:{a.id}", t("cancel.keep", lang))],
    )
    return "confirmation_sent"


async def pick_appointment(ctx: Ctx, action: str) -> None:
    """Cancel / reschedule entry: one appointment -> go straight on; several -> list to choose."""
    items = await upcoming(ctx)
    if not items:
        await say(ctx, "my.none")
        return
    if len(items) == 1:
        if action == "cancel":
            await ask_cancel(ctx, str(items[0].id))
        else:
            await begin_reschedule(ctx, str(items[0].id))
        return
    lang = ctx.lang
    ctx.state = S.CHOOSING_CANCEL if action == "cancel" else S.IDLE
    await send_list(
        ctx,
        t("cancel.pick" if action == "cancel" else "resched.pick", lang),
        t("late.ask_button", lang),
        [(f"{'cancel_req' if action == 'cancel' else 'resched_req'}:{a.id}", fmt_short(a.start_utc), a.token_label) for a in items],
        t("slots.section", lang),
    )


async def do_cancel(ctx: Ctx, appt_id: str) -> None:
    a = await own_appointment(ctx, appt_id)
    if a is None:
        await say(ctx, "stale")
        return
    try:
        await change_service.cancel(a.id, by="patient", actor="patient", patient_id=ctx.patient.id)
    except change_service.NotAllowed:
        await say(ctx, "cancel.not_allowed")
    except change_service.TooLate:
        await say(ctx, "cancel.too_late", phone=get_clinic().phone)
    except DomainError:
        await say(ctx, "stale")
    else:
        await outbox.process_outbox()
    ctx.state = S.IDLE


async def begin_reschedule(ctx: Ctx, appt_id: str) -> str:
    a = await own_appointment(ctx, appt_id)
    if a is None or a.status != "booked":
        await say(ctx, "stale")
        return "not_found"
    if a.queue_status in change_service.BLOCKED_FOR_PATIENT:
        await say(ctx, "cancel.not_allowed")
        return "not_allowed"
    ctx.context["resched_appt_id"] = str(a.id)
    today = local_today(clock.now())
    items = await find_and_send_slots(ctx, today, today + timedelta(days=7))
    if not items:
        ctx.context.pop("resched_appt_id", None)
        await say(ctx, "slots.none")
        return "no_slots"
    return "slots_sent"


async def begin_reschedule_confirm(ctx: Ctx, start: datetime) -> None:
    cfg = get_clinic()
    appt_id = ctx.context.get("resched_appt_id")
    draft = ctx.new_draft("resched", start, appt_id=appt_id)
    _, _, _, label = tokens.slot_token(cfg, start)
    lang = ctx.lang
    ctx.state = S.CONFIRMING_RESCHEDULE
    await send_buttons(
        ctx,
        t("resched.confirm_body", lang, date=fmt_date(start), time=fmt_time(start), token=label),
        [(f"confirm_resched:{draft['id']}", t("resched.confirm_btn", lang)), (f"change_slot:{draft['id']}", t("book.change_btn", lang))],
    )


async def confirm_reschedule(ctx: Ctx, draft_id: str) -> None:
    draft, expired = ctx.get_draft(draft_id)
    if draft is None or draft.get("kind") != "resched":
        await say(ctx, "stale")
        return
    if expired:
        ctx.clear_draft()
        await say(ctx, "book.expired")
        await resend_last_search(ctx, intro_key="slots.list_body")
        return
    try:
        await change_service.reschedule(
            uuid.UUID(draft["appt_id"]), datetime.fromisoformat(draft["start"]), by="patient", actor="patient",
            patient_id=ctx.patient.id,
        )
    except (booking_service.SlotTaken, booking_service.InvalidSlot):
        ctx.clear_draft()
        await resend_last_search(ctx)
        return
    except change_service.NotAllowed:
        await say(ctx, "cancel.not_allowed")
    except change_service.TooLate:
        await say(ctx, "cancel.too_late", phone=get_clinic().phone)
    except DomainError:
        await say(ctx, "book.failed", phone=get_clinic().phone)
    else:
        await outbox.process_outbox()
    ctx.clear_draft()
    ctx.context.pop("resched_appt_id", None)
    ctx.state = S.IDLE


# ------------------------------------------------------------------- queue


async def queue_status(ctx: Ctx) -> dict | None:
    async with get_session() as db:
        info = await queue_service.queue_status_for(db, ctx.patient.id)
    if info is None:
        return None
    info.pop("appointment")
    return info


async def say_queue_status(ctx: Ctx) -> None:
    info = await queue_status(ctx)
    if info is None:
        await say(ctx, "queue.none_today")
        return
    lang = ctx.lang
    await say(
        ctx, "queue.status", token=info["token"], status=t(f"queue.status.{info['queue_status']}", lang),
        ahead=info["ahead"], current=info["current"] or t("queue.nobody", lang), eta=info["eta"] or "-",
    )


async def report_late(ctx: Ctx, minutes: int, appt_id: str | None = None) -> str:
    cfg = get_clinic()
    a = await own_appointment(ctx, appt_id) if appt_id else None
    if a is None:
        async with get_session() as db:
            a = await queue_service.todays_appointment(db, ctx.patient.id)
    if a is None or a.status != "booked":
        await say(ctx, "late.no_appt")
        return "no_appointment_today"
    await queue_service.report_running_late(a.id, minutes)
    if minutes > cfg.queue.max_late_minutes:
        await say(ctx, "late.ack_long", max_late=cfg.queue.max_late_minutes)
    else:
        await say(ctx, "late.ack", grace=cfg.queue.grace_minutes, reinsert=cfg.queue.late_reinsert_after)
    return "recorded"


async def ask_late_minutes(ctx: Ctx, appt_id: str) -> None:
    lang = ctx.lang
    rows = [(f"late_min:{appt_id}:{m}", t(f"late.min{m}", lang), "") for m in (10, 20, 30, 45)]
    await send_list(ctx, t("late.ask_minutes", lang), t("late.ask_button", lang), rows, t("slots.section", lang))


# --------------------------------------------------------------------- info


def clinic_info_text(topic: str, lang: str) -> str:
    cfg = get_clinic()
    if topic == "address":
        return t("info.address", lang, address=cfg.address, maps=cfg.maps_link)
    if topic == "fee":
        return t("info.fee", lang, fee=cfg.consultation_fee_text)
    if topic == "phone":
        return t("info.phone", lang, phone=cfg.phone)
    if topic == "hours":
        lines = []
        for day in WEEKDAY_NAMES:
            ranges = cfg.hours.get(day, [])
            txt = ", ".join(f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}" for a, b in ranges) or t("info.closed", lang)
            lines.append(f"{day.title()}: {txt}")
        return t("info.hours", lang, hours="\n".join(lines))
    return t("info.general", lang, clinic=cfg.clinic_name, doctor=cfg.doctor_name, address=cfg.address,
             phone=cfg.phone, fee=cfg.consultation_fee_text)


async def handoff(ctx: Ctx, reason: str) -> None:
    ctx.handoff = True
    ctx.state = S.HANDOFF
    async with get_session() as db:
        outbox.notify_staff(db, "handoff.staff_alert", who=who(ctx), reason=reason[:80])
        await db.commit()
    await say(ctx, "handoff.patient")
    await outbox.process_outbox()


async def waitlist(ctx: Ctx, d: date, part: str = "any") -> dict:
    try:
        pos = await offers.join_waitlist(ctx.patient.id, d, part or "any")
    except offers.WaitlistLimit as exc:
        await say(ctx, "waitlist.limit", max=exc.ctx["max"])
        return {"joined": False, "reason": "limit"}
    await say(ctx, "waitlist.joined", date=fmt_date(d), position=pos)
    return {"joined": True, "position": pos}


async def followup_book(ctx: Ctx, fu_id: str) -> None:
    try:
        fid = uuid.UUID(fu_id)
    except ValueError:
        await say(ctx, "stale")
        return
    async with get_session() as db:
        fu = await db.get(FollowUp, fid)
    if fu is None or fu.patient_id != ctx.patient.id:
        await say(ctx, "stale")
        return
    ctx.context["followup_id"] = str(fu.id)
    today = local_today(clock.now())
    start = max(today, fu.due_date - timedelta(days=1))
    if not await find_and_send_slots(ctx, start, start + timedelta(days=5)):
        await say(ctx, "slots.none")
