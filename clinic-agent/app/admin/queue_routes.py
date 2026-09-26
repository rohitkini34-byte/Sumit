"""Live queue board for reception (HTMX, 10s refresh) and the waiting-room display."""
from __future__ import annotations

import secrets
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.admin.auth import render, verify_csrf
from app.admin.routes import admin_user
from app.calendar.slots import session_windows
from app.config import get_clinic, get_settings
from app.db.models import Appointment, Patient, SlotOffer
from app.db.session import get_session
from app.dev import clock
from app.scheduling import change_service, outbox, queue_service
from app.scheduling.common import DomainError, first_name, patient_name
from app.timeutil import fmt_time, local_today, minutes_between

router = APIRouter()


def _default_session(d: date) -> str:
    wins = session_windows(get_clinic(), d, include_holidays=True)
    now = clock.now()
    for w in wins:
        if now < w.end_utc + timedelta(minutes=30):
            return w.name
    return wins[-1].name if wins else next(iter(get_clinic().queue.sessions))


async def board_data(d: date, session: str) -> dict:
    now = clock.now()
    async with get_session() as db:
        appts, etas = await queue_service.session_etas(db, d, session)
        state = await queue_service.get_state(db, d, session)
        patients = {
            p.id: p
            for p in (
                await db.scalars(select(Patient).where(Patient.id.in_([a.patient_id for a in appts] or [uuid.uuid4()])))
            ).all()
        }
        offers = (
            await db.scalars(
                select(SlotOffer).where(SlotOffer.session_date == d, SlotOffer.session == session, SlotOffer.status == "open")
            )
        ).all()
        await db.commit()
    live = sorted((a for a in appts if a.queue_status in queue_service.LIVE), key=queue_service.order_key)
    others = sorted((a for a in appts if a.queue_status not in queue_service.LIVE), key=lambda a: a.start_utc)
    rows = []
    for a in live + others:
        eta = etas.get(a.id)
        p = patients.get(a.patient_id)
        rows.append({
            "id": str(a.id),
            "token": a.token_label,
            "name": (first_name(p) if p else "") or "—",
            "slot": fmt_time(a.start_utc) if not a.is_walk_in else "walk-in",
            "status": a.queue_status,
            "eta": f"{fmt_time(eta.window_start)}–{fmt_time(eta.window_end)}" if eta else "",
            "late_min": a.patient_reported_late_minutes,
            "hint": bool(a.arrived_hint_at) and a.queue_status in ("scheduled", "running_late"),
            "priority": a.priority,
            "displaced": a.displacement_count,
            "moved_from": a.previous_token_label,
            "flag": a.needs_staff_decision,
            "live": a.queue_status in queue_service.LIVE,
        })
    current = next((a for a in appts if a.queue_status == "in_consultation"), None)
    return {
        "rows": rows,
        "current_id": str(current.id) if current else queue_service.NOBODY,
        "current_token": current.token_label if current else None,
        "state": state,
        "offers": [
            {"type": o.offer_type, "slot": fmt_time(o.slot_start_utc),
             "left": max(0, int(minutes_between(now, o.expires_at)))} for o in offers
        ],
        "now": fmt_time(now),
    }


@router.get("/admin/queue")
async def queue_board(request: Request, user: str = Depends(admin_user), day: str | None = None, session: str | None = None,
                      note: str | None = None):
    d = date.fromisoformat(day) if day else local_today(clock.now())
    session = session or _default_session(d)
    sessions = [w.name for w in session_windows(get_clinic(), d, include_holidays=True)]
    data = await board_data(d, session)
    return render(request, "queue.html", day=d.isoformat(), session=session, sessions=sessions, note=note, **data)


@router.get("/admin/queue/board")
async def queue_board_partial(request: Request, day: str, session: str, user: str = Depends(admin_user)):
    data = await board_data(date.fromisoformat(day), session)
    return render(request, "_queue_board.html", day=day, session=session, **data)


def _back(day: str, session: str, note: str | None = None) -> RedirectResponse:
    url = f"/admin/queue?day={day}&session={session}"
    if note:
        url += f"&note={note}"
    return RedirectResponse(url, status_code=303)


@router.post("/admin/queue/action")
async def queue_action(
    request: Request,
    action: str = Form(...),
    day: str = Form(...),
    session: str = Form(...),
    appt_id: str = Form(""),
    expected_current: str = Form(queue_service.ANY),
    minutes: int = Form(0),
    name: str = Form(""),
    phone: str = Form(""),
    user: str = Depends(admin_user),
):
    await verify_csrf(request)
    actor = f"admin:{user}"
    d = date.fromisoformat(day)
    note = None
    try:
        if action == "check_in":
            res = await queue_service.check_in(uuid.UUID(appt_id), actor)
            note = res.note
        elif action == "call_next":
            await queue_service.call_next(d, session, actor, expected_current=expected_current)
        elif action == "done":
            await queue_service.mark_done(uuid.UUID(appt_id), actor)
        elif action == "priority":
            await queue_service.set_priority(uuid.UUID(appt_id), actor, True)
        elif action == "unpriority":
            await queue_service.set_priority(uuid.UUID(appt_id), actor, False)
        elif action == "walk_in":
            if d != local_today(clock.now()):
                note = "Walk-ins can only be added to today's queue."
            else:
                await queue_service.add_walk_in(d, session, actor, name=name, phone=phone or None)
        elif action == "delay":
            await queue_service.set_delay(d, session, minutes, actor)
        elif action == "pause":
            await queue_service.set_delay(d, session, minutes, actor, paused=True)
        elif action == "resume":
            await queue_service.set_delay(d, session, 0, actor, paused=False)
        elif action == "close":
            await queue_service.close_session(d, session, actor)
        elif action == "cancel":
            await change_service.cancel(uuid.UUID(appt_id), by="staff", actor=actor, reason_code="staff")
        elif action == "see_at_end":
            async with get_session() as db:
                a = await db.get(Appointment, uuid.UUID(appt_id))
                a.needs_staff_decision = None
                await db.commit()
        elif action == "move":
            return RedirectResponse(f"/admin/appointments/{appt_id}/move", status_code=303)
    except DomainError as exc:
        note = type(exc).__name__
    await outbox.process_outbox()
    if request.headers.get("HX-Request"):
        data = await board_data(d, session)
        return render(request, "_queue_board.html", day=day, session=session, note=note, **data)
    return _back(day, session, note)


# ---------------------------------------------------------- waiting-room TV


@router.get("/queue/display/{key}")
async def display(request: Request, key: str, session: str | None = None, partial: int = 0):
    if not secrets.compare_digest(key, get_settings().QUEUE_DISPLAY_KEY):
        raise HTTPException(status_code=404)
    d = local_today(clock.now())
    session = session or _default_session(d)
    data = await board_data(d, session)
    waiting = [r["token"] for r in data["rows"] if r["status"] in ("checked_in", "scheduled", "running_late")][:3]
    ctx = {"now_serving": data["current_token"], "next_tokens": waiting, "key": key, "session": session}
    # tokens only, never names
    return render(request, "_display_inner.html" if partial else "display.html", **ctx)
