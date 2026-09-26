"""Dev-only routes (mounted only when APP_ENV=dev): WhatsApp simulator, clock, jobs,
fake calendar busy blocks, and the queue simulator."""
from __future__ import annotations

import json
import time as _time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select

from app.calendar.base import get_calendar
from app.calendar.slots import session_grid, session_windows
from app.config import get_clinic, get_settings
from app.db.crypto import encrypt
from app.db.models import Appointment, Patient, QueueEvent, SessionState
from app.db.session import get_session
from app.dev import clock
from app.scheduling import booking_service, change_service, outbox, queue_service, reminders
from app.scheduling.common import get_or_create_patient, patient_phone
from app.timeutil import fmt_short, fmt_time, local_to_utc, local_today, to_local
from app.whatsapp.client import get_wa
from app.whatsapp.signature import compute_signature

router = APIRouter(prefix="/dev")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _page(request: Request, name: str, **ctx):
    ctx.setdefault("now_local", to_local(clock.now()).strftime("%a %d %b %Y %H:%M"))
    ctx.setdefault("clock_overridden", clock.is_overridden())
    return templates.TemplateResponse(request, name, ctx)


@router.get("", response_class=HTMLResponse)
async def index(request: Request):
    return _page(request, "dev_index.html", settings=get_settings())


# ------------------------------------------------------------ chat simulator


@router.get("/chat", response_class=HTMLResponse)
async def chat(request: Request):
    return _page(request, "chat.html", app_secret=get_settings().WA_APP_SECRET, clinic=get_clinic())


def build_payload(phone: str, name: str, *, text: str | None = None, reply_id: str | None = None,
                  reply_title: str | None = None, kind: str = "text") -> dict:
    """Exact Meta webhook shape for an inbound message."""
    wa_id = phone.lstrip("+")
    msg: dict = {"from": wa_id, "id": f"wamid.sim.{uuid.uuid4().hex}", "timestamp": str(int(_time.time()))}
    if kind == "text":
        msg.update(type="text", text={"body": text or ""})
    elif kind == "list_reply":
        msg.update(type="interactive", interactive={"type": "list_reply", "list_reply": {"id": reply_id, "title": reply_title or ""}})
    elif kind == "button_reply":
        msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": reply_id, "title": reply_title or ""}})
    elif kind == "template_button":
        msg.update(type="button", button={"payload": reply_id, "text": reply_title or ""})
    else:
        msg.update(type=kind)
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": get_settings().WA_BUSINESS_ACCOUNT_ID or "WABA_DEV",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550000000", "phone_number_id": get_settings().WA_PHONE_NUMBER_ID or "PNID_DEV"},
                    "contacts": [{"profile": {"name": name or "Test User"}, "wa_id": wa_id}],
                    "messages": [msg],
                },
            }],
        }],
    }


@router.post("/api/sign")
async def sign(request: Request):
    """Fallback signer for browsers without WebCrypto (non-localhost http)."""
    raw = await request.body()
    return {"signature": compute_signature(raw, get_settings().WA_APP_SECRET)}


@router.get("/api/messages")
async def messages(phone: str):
    transport = get_wa().transport
    sent = getattr(transport, "for_phone", lambda p: [])(phone)
    return {"messages": [m.to_json() for m in sent]}


@router.post("/api/reset")
async def reset(phone: str):
    transport = get_wa().transport
    if hasattr(transport, "sent"):
        transport.sent[:] = [m for m in transport.sent if m.to != phone]
    return {"ok": True}


# ----------------------------------------------------------------- clock / jobs


@router.get("/clock", response_class=HTMLResponse)
async def clock_page(request: Request):
    return _page(request, "clock.html", value=to_local(clock.now()).strftime("%Y-%m-%dT%H:%M"))


@router.post("/clock")
async def clock_set(request: Request, action: str = Form(...), when: str = Form(""), minutes: int = Form(0)):
    if action == "set" and when:
        clock.set_now(datetime.fromisoformat(when).replace(tzinfo=get_settings().tz))
    elif action == "advance":
        clock.advance(minutes)
    elif action == "reset":
        clock.reset()
    return RedirectResponse("/dev/clock", status_code=303)


@router.post("/run-jobs")
async def run_jobs(request: Request):
    result = await reminders.run_all_jobs()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(result)
    return _page(request, "result.html", title="Jobs run", result=json.dumps(result, indent=2, default=str))


# ------------------------------------------------------------ fake calendar


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request):
    events = []
    if get_settings().CALENDAR_MODE == "fake":
        events = await get_calendar().list_all()
    rows = [{"id": e.event_id, "summary": e.summary, "start": fmt_short(e.start_utc), "end": fmt_time(e.end_utc),
             "appt": bool(e.appointment_id)} for e in events]
    today = local_today(clock.now())
    return _page(request, "calendar.html", rows=rows, today=today.isoformat(), mode=get_settings().CALENDAR_MODE)


@router.post("/calendar/busy")
async def add_busy(day: str = Form(...), start: str = Form(...), end: str = Form(...), summary: str = Form("Doctor busy")):
    d = date.fromisoformat(day)
    s = local_to_utc(d, datetime.strptime(start, "%H:%M").time())
    e = local_to_utc(d, datetime.strptime(end, "%H:%M").time())
    await get_calendar().add_busy_block(s, e, summary)
    return RedirectResponse("/dev/calendar", status_code=303)


@router.post("/calendar/delete")
async def del_event(event_id: str = Form(...)):
    await get_calendar().delete(event_id)
    return RedirectResponse("/dev/calendar", status_code=303)


# ------------------------------------------------------------- queue simulator

SIM_PREFIX = "+9199990"


def _next_open_day() -> tuple[date, str]:
    cfg = get_clinic()
    d = local_today(clock.now()) + timedelta(days=1)
    for _ in range(14):
        wins = session_windows(cfg, d)
        if wins:
            return d, wins[0].name
        d += timedelta(days=1)
    return d, next(iter(cfg.queue.sessions))


@router.get("/queue-sim", response_class=HTMLResponse)
async def queue_sim_page(request: Request):
    d, s = _next_open_day()
    for _ in range(30):
        if not await _real_bookings(d, s):
            break
        d += timedelta(days=1)
        wins = session_windows(get_clinic(), d)
        s = wins[0].name if wins else s
    cfg = get_clinic()
    default = "\n".join(["ontime", "early:20", "late:15", "ontime", "noshow", "late:60", "ontime", "cancel", "ontime", "late:5"])
    return _page(request, "queue_sim.html", day=d.isoformat(), session=s, arrivals=default, consult=cfg.slot_minutes,
                 results=None, queue_cfg=cfg.queue.model_dump(), events=None)


@router.post("/queue-sim", response_class=HTMLResponse)
async def queue_sim_run(request: Request, day: str = Form(...), session: str = Form(...), arrivals: str = Form(...),
                        consult: int = Form(15)):
    cfg = get_clinic()
    try:
        results, events = await run_queue_simulation(date.fromisoformat(day), session, arrivals.splitlines(), consult)
    except SimulationError as exc:
        return _page(request, "queue_sim.html", day=day, session=session, arrivals=arrivals, consult=consult,
                     results=None, queue_cfg=cfg.queue.model_dump(), events=None, error=str(exc))
    return _page(request, "queue_sim.html", day=day, session=session, arrivals=arrivals, consult=consult,
                 results=results, queue_cfg=cfg.queue.model_dump(), events=events)


class SimulationError(Exception):
    pass


async def _real_bookings(d: date, session: str) -> int:
    n = 0
    async with get_session() as db:
        appts = (await db.scalars(select(Appointment).where(
            Appointment.session_date == d, Appointment.session == session,
            Appointment.status.not_in(("cancelled", "rescheduled"))))).all()
        for a in appts:
            p = await db.get(Patient, a.patient_id)
            if not (patient_phone(p) or "").startswith(SIM_PREFIX):
                n += 1
    return n


async def _clear_sim(d: date, session: str) -> None:
    async with get_session() as db:
        appts = (await db.scalars(select(Appointment).where(Appointment.session_date == d, Appointment.session == session,
                                                             Appointment.status.in_(("booked", "completed", "no_show"))))).all()
        for a in appts:
            p = await db.get(Patient, a.patient_id)
            if (patient_phone(p) or "").startswith(SIM_PREFIX):
                a.status = "cancelled"
                a.queue_status = "cancelled"
        await db.execute(delete(SessionState).where(SessionState.session_date == d, SessionState.session == session))
        await db.commit()


async def run_queue_simulation(d: date, session: str, lines: list[str], consult_min: int):
    """Replay a session against the real queue_service / change_service with the dev clock."""
    cfg = get_clinic()
    saved_auto = outbox.AUTO_KICK
    outbox.AUTO_KICK = False
    saved_clock = clock.snapshot()
    if await _real_bookings(d, session):
        raise SimulationError("That session has real bookings. Pick a day/session with no bookings "
                              "(the simulator closes the session and would mark real patients as no-shows).")
    try:
        await _clear_sim(d, session)
        grid = session_grid(cfg, d, session)
        window = next(w for w in session_windows(cfg, d, include_holidays=True) if w.name == session)
        clock.set_now(window.start_utc - timedelta(hours=4))
        plan = []
        for i, raw in enumerate([ln.strip().lower() for ln in lines if ln.strip()][: len(grid)]):
            phone = f"{SIM_PREFIX}{i:05d}"
            async with get_session() as db:
                p, _ = await get_or_create_patient(db, phone)
                p.name_enc = encrypt(f"Sim Patient {i + 1}")
                p.consent_given_at = clock.now()
                await db.commit()
                pid = p.id
            appt = await booking_service.book(pid, grid[i], source="admin", actor="sim", enforce_limit=False)
            kind, _, n = raw.partition(":")
            n = int(n or 0)
            arrival = {
                "ontime": grid[i] - timedelta(minutes=5),
                "early": grid[i] - timedelta(minutes=n),
                "late": grid[i] + timedelta(minutes=n),
            }.get(kind)
            plan.append({"id": appt.id, "token": appt.token_label, "slot": grid[i], "kind": raw, "arrival": arrival,
                         "arrived": False})
        for p in plan:
            if p["kind"] == "cancel":
                clock.set_now(window.start_utc - timedelta(hours=2))
                await change_service.cancel(p["id"], by="patient", actor="sim")
        clock.set_now(window.start_utc - timedelta(minutes=15))
        end = window.end_utc + timedelta(hours=3)
        consult = timedelta(minutes=consult_min)
        started_at = None
        while clock.now() < end:
            now = clock.now()
            for p in plan:
                if p["arrival"] and not p["arrived"] and now >= p["arrival"] and p["kind"] != "cancel":
                    await queue_service.check_in(p["id"], "sim")
                    p["arrived"] = True
            async with get_session() as db:
                appts = await queue_service.load_session(db, d, session)
            current = next((a for a in appts if a.queue_status == "in_consultation"), None)
            waiting = [a for a in appts if a.queue_status in queue_service.WAITING]
            if now >= window.start_utc and (current is None or (started_at and now - started_at >= consult)):
                if current is not None or any(a.queue_status == "checked_in" for a in waiting):
                    res = await queue_service.call_next(d, session, "sim")
                    started_at = now if res.appointment else None
            if not waiting and current is None and all(p["arrived"] or not p["arrival"] or p["kind"] == "cancel" for p in plan) \
                    and now > window.start_utc:
                break
            clock.advance(1)
        await queue_service.close_session(d, session, "sim")
        results = []
        async with get_session() as db:
            for p in plan:
                a = await db.get(Appointment, p["id"])
                wait = None
                if a.called_at and p["arrival"]:
                    wait = int((a.called_at - max(p["arrival"], a.start_utc)).total_seconds() // 60)
                results.append({
                    "token": p["token"], "slot": fmt_time(p["slot"]), "plan": p["kind"],
                    "arrival": fmt_time(p["arrival"]) if p["arrival"] else "—",
                    "called": fmt_time(a.called_at) if a.called_at else "—",
                    "wait": wait, "status": a.queue_status, "displaced": a.displacement_count,
                })
            evs = (await db.execute(
                select(QueueEvent, Appointment.token_label).join(Appointment, Appointment.id == QueueEvent.appointment_id)
                .where(Appointment.session_date == d, Appointment.session == session, QueueEvent.actor == "sim")
                .order_by(QueueEvent.ts, QueueEvent.created_at)
            )).all()
            events = [{"ts": fmt_time(e.ts), "token": tok, "event": e.event, "meta": e.meta_json} for e, tok in evs]
        return results, events
    finally:
        clock.restore(saved_clock)
        outbox.AUTO_KICK = saved_auto
