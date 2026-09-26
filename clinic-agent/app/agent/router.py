"""Inbound message routing (section 8.1).

Order: dedupe -> opt-out/opt-in -> emergency -> data deletion -> consent -> handoff
-> structured replies (deterministic, no LLM) -> free text (deterministic shortcuts, then LLM).

Emergency detection runs before the consent gate: emergency guidance must reach a
first-time sender too, and it stores nothing.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from app.agent import flows, safety
from app.agent import state as S
from app.agent.llm import get_llm, run_agent
from app.agent.state import Ctx
from app.config import get_clinic
from app.db.models import Appointment, ProcessedMessage
from app.db.session import get_session
from app.dev import clock
from app.i18n import detect_language_rule_based, has_devanagari, heuristic_hi_mr, t
from app.scheduling import offers, outbox, queue_service, reminders
from app.scheduling.common import get_or_create_patient
from app.timeutil import local_today
from app.whatsapp.client import get_wa
from app.whatsapp.parser import InboundMessage

log = logging.getLogger(__name__)
CONSENT_VERSION = "2026-09-v1"


async def _dedupe(msg: InboundMessage) -> bool:
    """True if this wa_message_id was already processed (Meta retries)."""
    async with get_session() as db:
        db.add(ProcessedMessage(wa_message_id=msg.wa_message_id))
        try:
            await db.commit()
        except IntegrityError:
            return True
    return False


async def _touch_patient(msg: InboundMessage) -> uuid.UUID:
    async with get_session() as db:
        p, _ = await get_or_create_patient(db, msg.from_phone)
        p.last_inbound_at = clock.now()
        await db.commit()
        return p.id


async def _detect_language(text: str) -> str:
    if not has_devanagari(text):
        return "en"
    guess = heuristic_hi_mr(text)
    if guess:
        return guess
    return await get_llm().classify_hi_mr(text) or detect_language_rule_based(text)


async def handle_inbound(msg: InboundMessage) -> None:
    if not msg.wa_message_id or await _dedupe(msg):
        return
    patient_id = await _touch_patient(msg)
    await get_wa().mark_as_read(msg.wa_message_id)
    ctx = await Ctx.load(patient_id)
    try:
        await _route(ctx, msg)
    finally:
        if ctx.patient.anonymised_at is None:
            await ctx.save()


async def _route(ctx: Ctx, msg: InboundMessage) -> None:
    cfg = get_clinic()
    text = (msg.text or "").strip()

    if not msg.supported:
        if not ctx.patient.opted_out:
            await flows.say(ctx, "unsupported_type", phone=cfg.phone)
        return

    # 1. opt-out / opt-in
    if text and safety.is_stop(text):
        await ctx.update_patient(opted_out=True)
        await flows.send_text(ctx, t("optout.ack", ctx.lang), allow_opted_out=True)
        return
    if text and safety.is_start(text):
        if ctx.patient.opted_out:
            await ctx.update_patient(opted_out=False)
            await flows.say(ctx, "optin.ack")
            return
    if ctx.patient.opted_out:
        return

    # 2. emergency: deterministic, never the LLM
    if text and safety.is_emergency(text):
        if has_devanagari(text) and ctx.patient.consent_given_at is None:
            await ctx.update_patient(preferred_language=await _detect_language(text))
        await flows.say(ctx, "emergency.body", phone=cfg.phone)
        async with get_session() as db:
            outbox.notify_staff(db, "emergency.staff_alert", who=flows.who(ctx))
            await db.commit()
        await outbox.process_outbox()
        return

    # data deletion (DPDP)
    if text and safety.is_delete_request(text):
        await flows.say(ctx, "delete.done")
        await reminders.delete_patient_data(ctx.patient.id)
        await ctx.refresh_patient()
        return

    # 3. consent gate
    if ctx.patient.consent_given_at is None:
        await _consent(ctx, msg, text)
        return

    # 4. human handoff
    if ctx.handoff:
        ctx.add_turn("user", text or f"[{msg.reply_title}]")
        async with get_session() as db:
            outbox.notify_staff(db, "handoff.forward", who=flows.who(ctx))
            await db.commit()
        await outbox.process_outbox()
        return

    # 5. structured replies
    if msg.is_structured:
        ctx.add_turn("user", f"[tapped: {msg.reply_title or msg.reply_id}]")
        await handle_reply(ctx, msg.reply_id or "")
        _record_bot_turns(ctx)
        return

    # 6. free text
    if has_devanagari(text) and ctx.lang == "en":
        await ctx.update_patient(preferred_language=await _detect_language(text))
    if ctx.state == S.ASKING_NAME and ctx.context.get("pending_slot"):
        await flows.name_received(ctx, text)
    elif safety.is_greeting(text):
        await flows.send_menu(ctx)
    elif safety.is_arrived(text):
        await _arrived(ctx)
    else:
        reply = await run_agent(ctx, text)  # history = earlier turns; the current text is appended there
        if reply:
            await flows.send_text(ctx, reply)
    ctx.add_turn("user", text)
    _record_bot_turns(ctx)


def _record_bot_turns(ctx: Ctx) -> None:
    if ctx.sent:
        ctx.add_turn("assistant", "\n".join(ctx.sent[-2:]))


async def _consent(ctx: Ctx, msg: InboundMessage, text: str) -> None:
    cfg = get_clinic()
    rid = msg.reply_id or ""
    if rid == "consent_yes":
        await ctx.update_patient(consent_given_at=clock.now(), consent_version=CONSENT_VERSION)
        ctx.state = S.IDLE
        await flows.say(ctx, "consent.thanks")
        await flows.send_menu(ctx)
        return
    if rid == "consent_no":
        await flows.say(ctx, "consent.declined", phone=cfg.phone)
        async with get_session() as db:
            from app.db.models import Patient

            p = await db.get(Patient, ctx.patient.id)
            await reminders.anonymise_patient(db, p, "patient")
            await db.commit()
        await ctx.refresh_patient()
        return
    if ctx.state in (S.NEW,) and text:
        await ctx.update_patient(preferred_language=await _detect_language(text))
    ctx.state = S.AWAITING_CONSENT
    lang = ctx.lang
    await flows.send_buttons(
        ctx,
        t("consent.body", lang, clinic=cfg.clinic_name),
        [("consent_yes", t("consent.agree", lang)), ("consent_no", t("consent.decline", lang))],
    )


async def _arrived(ctx: Ctx) -> None:
    async with get_session() as db:
        a = await queue_service.todays_appointment(db, ctx.patient.id)
    if a is not None and a.status == "booked":
        await queue_service.mark_arrived_hint(a.id)  # a hint for reception, never a check-in
    await flows.say(ctx, "arrived.ack")


# ------------------------------------------------------------ structured replies


async def handle_reply(ctx: Ctx, reply_id: str) -> None:
    action, _, arg = reply_id.partition(":")
    lang = ctx.lang
    cfg = get_clinic()
    today = local_today(clock.now())

    if action == "menu":
        if arg == "book":
            await _start_new_booking(ctx)
        elif arg == "my":
            await flows.say_appointments(ctx)
        elif arg == "info":
            await flows.send_text(ctx, flows.clinic_info_text("general", lang))
        return
    if action in ("consent_yes", "consent_no"):
        await flows.send_menu(ctx)
        return
    if action == "slot":
        if ctx.state not in (S.CHOOSING_SLOT, S.RESCHEDULE_CHOOSING_SLOT, S.CONFIRMING_BOOKING,
                             S.CONFIRMING_RESCHEDULE, S.ASKING_NAME, S.IDLE):
            await flows.say(ctx, "stale")
            return
        await flows.slot_chosen(ctx, arg)
        return
    if action == "confirm_book":
        await flows.confirm_booking(ctx, arg)
        return
    if action == "confirm_resched":
        await flows.confirm_reschedule(ctx, arg)
        return
    if action == "change_slot":
        ctx.clear_draft()
        await flows.resend_last_search(ctx, intro_key="slots.list_body")
        return
    if action in ("cancel_req", "r24_cancel", "r2_cancel", "delay_cancel"):
        await flows.ask_cancel(ctx, arg)
        return
    if action == "cancel_yes":
        await flows.do_cancel(ctx, arg)
        return
    if action == "cancel_no":
        ctx.state = S.IDLE
        await flows.say(ctx, "cancel.kept")
        return
    if action in ("resched_req", "r24_resched", "missed_resched"):
        await flows.begin_reschedule(ctx, arg)
        return
    if action == "r24_confirm":
        a = await flows.own_appointment(ctx, arg)
        if a is None or a.status != "booked":
            await flows.say(ctx, "stale")
            return
        async with get_session() as db:
            row = await db.get(Appointment, a.id)
            row.confirmed_by_patient_at = clock.now()
            await db.commit()
        await flows.say(ctx, "confirm24.ack")
        return
    if action in ("r2_onway", "missed_coming"):
        if await flows.own_appointment(ctx, arg) is None:
            await flows.say(ctx, "stale")
            return
        await flows.say(ctx, "onway.ack")
        return
    if action == "r2_late":
        if await flows.own_appointment(ctx, arg) is None:
            await flows.say(ctx, "stale")
            return
        await flows.ask_late_minutes(ctx, arg)
        return
    if action == "late_min":
        appt_id, _, minutes = arg.partition(":")
        await flows.report_late(ctx, int(minutes) if minutes.isdigit() else 15, appt_id)
        return
    if action == "delay_ok":
        await flows.say(ctx, "delay_ok.ack")
        return
    if action in ("book_again", "sc_resched"):
        await _start_new_booking(ctx)
        return
    if action == "sc_cancel":
        await flows.say(ctx, "fu.not_now")
        return
    if action == "fu_book":
        await flows.followup_book(ctx, arg)
        return
    if action == "fu_no":
        await flows.say(ctx, "fu.not_now")
        return
    if action in ("offer_yes", "wl_yes"):
        try:
            await offers.accept_offer(uuid.UUID(arg), ctx.patient.id)
        except (offers.OfferUnavailable, ValueError):
            await flows.say(ctx, "offer.taken")
            return
        except Exception as exc:  # e.g. SlotTaken / BookingLimit from the booking path
            log.info("offer accept failed: %s", type(exc).__name__)
            await flows.say(ctx, "offer.taken")
            return
        await outbox.process_outbox()
        return
    if action in ("offer_no", "wl_no"):
        try:
            await offers.decline_offer(uuid.UUID(arg), ctx.patient.id)
        except (offers.OfferUnavailable, ValueError):
            pass
        await flows.say(ctx, "offer.declined" if action == "offer_no" else "waitlist.declined")
        await outbox.process_outbox()
        return
    await flows.say(ctx, "stale")


async def _start_new_booking(ctx: Ctx) -> None:
    ctx.context.pop("resched_appt_id", None)
    today = local_today(clock.now())
    items = await flows.find_and_send_slots(ctx, today, today + timedelta(days=3))
    if not items:
        items = await flows.find_and_send_slots(ctx, today, today + timedelta(days=get_clinic().booking_horizon_days))
    if not items:
        await flows.say(ctx, "slots.none")
