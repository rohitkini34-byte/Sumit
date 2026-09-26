"""LLM tools (section 8.3): JSON schemas + implementations.

Tools act only for ctx.patient (from the session). IDs from the model are validated
server-side (signed slot ids, appointment ownership), so prompt injection can't reach
other patients' data or bypass the Confirm button.
"""
from __future__ import annotations

import logging
from datetime import date, time, timedelta

from app.agent import flows
from app.agent import state as S
from app.agent.state import Ctx
from app.config import get_clinic
from app.dev import clock
from app.i18n import t
from app.timeutil import local_today

log = logging.getLogger(__name__)

_DATE = {"type": "string", "description": "YYYY-MM-DD (local date, Asia/Kolkata)"}
_PART = {"type": "string", "enum": ["morning", "evening", "any"]}

TOOLS: list[dict] = [
    {
        "name": "get_available_slots",
        "description": "Find free appointment slots and send them to the patient as a tappable list. "
        "Returns up to 10 slots. Use not_before for requests like 'after 6'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": _DATE,
                "date_to": _DATE,
                "part_of_day": _PART,
                "not_before": {"type": "string", "description": "HH:MM 24h local, optional"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "start_booking",
        "description": "Prepare (NOT make) a booking for a slot the patient chose. Sends Confirm/Change buttons; "
        "the booking happens only when the patient taps Confirm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slot_id": {"type": "string", "description": "slot_id from get_available_slots"},
                "patient_name": {"type": "string"},
                "age": {"type": "integer"},
                "visit_reason_short": {"type": "string", "description": "a few words at most"},
            },
            "required": ["slot_id", "patient_name"],
        },
    },
    {
        "name": "list_my_appointments",
        "description": "The patient's upcoming appointments with token and approximate expected time.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_my_queue_status",
        "description": "Today's live queue status: token, status, patients ahead, token with the doctor, ETA window.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_running_late",
        "description": "Record that the patient will be late today and tell them the late-arrival rules.",
        "input_schema": {
            "type": "object",
            "properties": {"minutes_late": {"type": "integer", "minimum": 1, "maximum": 240}},
            "required": ["minutes_late"],
        },
    },
    {
        "name": "request_cancel",
        "description": "Send Yes cancel / Keep buttons for one of the patient's appointments. Omit appointment_id "
        "to let the patient choose.",
        "input_schema": {"type": "object", "properties": {"appointment_id": {"type": "string"}}},
    },
    {
        "name": "request_reschedule",
        "description": "Start moving one of the patient's appointments: sends fresh slots to choose from. Omit "
        "appointment_id to let the patient choose.",
        "input_schema": {"type": "object", "properties": {"appointment_id": {"type": "string"}}},
    },
    {
        "name": "join_waitlist",
        "description": "Only when no slots are free that day: add the patient to that day's waitlist.",
        "input_schema": {"type": "object", "properties": {"date": _DATE, "part_of_day": _PART}, "required": ["date"]},
    },
    {
        "name": "get_clinic_info",
        "description": "Clinic address, hours, fee or phone, from the clinic configuration.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "enum": ["address", "hours", "fee", "phone", "general"]}},
            "required": ["topic"],
        },
    },
    {
        "name": "handoff_to_human",
        "description": "Pass the conversation to clinic staff.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "set_language",
        "description": "Change the patient's preferred language for fixed messages.",
        "input_schema": {
            "type": "object",
            "properties": {"lang": {"type": "string", "enum": ["en", "hi", "mr"]}},
            "required": ["lang"],
        },
    },
]
TOOL_NAMES = {x["name"] for x in TOOLS}


class ToolError(Exception):
    pass


def _date(v, field: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError as exc:
        raise ToolError(f"{field} must be YYYY-MM-DD") from exc


async def execute(name: str, args: dict, ctx: Ctx) -> dict:
    if name not in TOOL_NAMES:
        raise ToolError(f"unknown tool {name}")
    return await globals()[f"_t_{name}"](args or {}, ctx)


async def _t_get_available_slots(a: dict, ctx: Ctx) -> dict:
    cfg = get_clinic()
    today = local_today(clock.now())
    d_from = _date(a.get("date_from", today.isoformat()), "date_from")
    d_to = _date(a.get("date_to", a.get("date_from", today.isoformat())), "date_to")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if d_from > today + timedelta(days=cfg.booking_horizon_days):
        return {"slots": [], "say": f"Bookings open only {cfg.booking_horizon_days} days ahead."}
    part = a.get("part_of_day") if a.get("part_of_day") in ("morning", "evening") else None
    not_before = None
    if a.get("not_before"):
        try:
            not_before = time.fromisoformat(a["not_before"])
        except ValueError:
            raise ToolError("not_before must be HH:MM")
    if ctx.state not in (S.RESCHEDULE_CHOOSING_SLOT, S.CONFIRMING_RESCHEDULE):
        ctx.context.pop("resched_appt_id", None)
    items = await flows.find_and_send_slots(ctx, d_from, d_to, part, not_before)
    if not items:
        return {"slots": [], "sent_to_patient": False, "say": t("slots.none", ctx.lang),
                "hint": "offer another day or join_waitlist"}
    return {"slots": items, "sent_to_patient": True, "say": t("mock.slots_sent", ctx.lang),
            "note": "slots sent as list"}


async def _t_start_booking(a: dict, ctx: Ctx) -> dict:
    start = flows.slot_from_id(str(a.get("slot_id", "")))
    if start is None:
        raise ToolError("invalid slot_id: use a slot_id returned by get_available_slots")
    name = " ".join(str(a.get("patient_name", "")).split())[:60]
    if len(name) < 2:
        raise ToolError("patient_name is required")
    age = a.get("age")
    if age is not None and not (0 <= int(age) <= 120):
        age = None
    if ctx.context.get("resched_appt_id"):
        await flows.begin_reschedule_confirm(ctx, start)
        return {"status": "reschedule_confirmation_buttons_sent"}
    await flows.begin_booking_confirm(ctx, start, name, int(age) if age is not None else None,
                                      a.get("visit_reason_short"))
    return {"status": "confirmation_buttons_sent", "booked": False,
            "note": "Not booked yet: the patient must tap Confirm."}


async def _t_list_my_appointments(a: dict, ctx: Ctx) -> dict:
    return {"appointments": await flows.list_appointments(ctx)}


async def _t_get_my_queue_status(a: dict, ctx: Ctx) -> dict:
    info = await flows.queue_status(ctx)
    return info or {"today": None, "say": t("queue.none_today", ctx.lang)}


async def _t_report_running_late(a: dict, ctx: Ctx) -> dict:
    minutes = int(a.get("minutes_late", 15))
    status = await flows.report_late(ctx, max(1, min(minutes, 240)))
    return {"status": status, "sent_to_patient": True}


async def _t_request_cancel(a: dict, ctx: Ctx) -> dict:
    if not a.get("appointment_id"):
        await flows.pick_appointment(ctx, "cancel")
        return {"status": "choice_sent"}
    return {"status": await flows.ask_cancel(ctx, a["appointment_id"])}


async def _t_request_reschedule(a: dict, ctx: Ctx) -> dict:
    if not a.get("appointment_id"):
        await flows.pick_appointment(ctx, "reschedule")
        return {"status": "choice_sent"}
    return {"status": await flows.begin_reschedule(ctx, a["appointment_id"])}


async def _t_join_waitlist(a: dict, ctx: Ctx) -> dict:
    d = _date(a.get("date"), "date")
    part = a.get("part_of_day") or "any"
    from app.db.session import get_session
    from app.scheduling import booking_service

    async with get_session() as db:
        free = await booking_service.find_slots(db, d, d, part if part != "any" else None, limit=1)
    if free:
        await flows.say(ctx, "waitlist.has_slots")
        await flows.find_and_send_slots(ctx, d, d, part if part != "any" else None)
        return {"joined": False, "reason": "slots_available", "sent_to_patient": True}
    return {**await flows.waitlist(ctx, d, part), "sent_to_patient": True}


async def _t_get_clinic_info(a: dict, ctx: Ctx) -> dict:
    topic = a.get("topic", "general")
    return {"info": flows.clinic_info_text(topic, ctx.lang)}


async def _t_handoff_to_human(a: dict, ctx: Ctx) -> dict:
    await flows.handoff(ctx, str(a.get("reason", "requested"))[:80])
    return {"status": "handed_off", "sent_to_patient": True}


async def _t_set_language(a: dict, ctx: Ctx) -> dict:
    lang = a.get("lang")
    if lang not in ("en", "hi", "mr"):
        raise ToolError("lang must be en, hi or mr")
    await ctx.update_patient(preferred_language=lang)
    return {"status": "ok", "say": t("lang.set", lang)}
