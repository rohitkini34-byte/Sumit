"""Admin UI: appointments, follow-ups, handoff inbox, manual booking, session tools."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.admin.auth import LoginRequired, check_password, current_user, login_limiter, render, verify_csrf
from app.agent import state as S
from app.calendar.base import get_calendar
from app.calendar.slots import session_windows
from app.config import get_clinic
from app.db.crypto import encrypt
from app.db.models import Appointment, AuditLog, Conversation, FollowUp, Patient
from app.db.session import get_session
from app.dev import clock
from app.scheduling import booking_service, change_service, outbox, queue_service
from app.scheduling.common import DomainError, audit, first_name, get_or_create_patient, patient_name, recipient
from app.timeutil import fmt_date, fmt_short, fmt_time, local_to_utc, local_today
from app.whatsapp.client import SendBlocked, get_wa

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def admin_user(request: Request) -> str:
    return current_user(request)


# ------------------------------------------------------------------ login


@router.get("/admin/login")
async def login_page(request: Request):
    return render(request, "login.html", error=None)


@router.post("/admin/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    await verify_csrf(request)
    ip = request.client.host if request.client else "?"
    if not login_limiter.allow(ip):
        return render(request, "login.html", error="Too many attempts. Wait a minute.")
    if not check_password(username, password):
        return render(request, "login.html", error="Wrong username or password.")
    request.session.clear()
    request.session["user"] = username
    async with get_session() as db:
        await audit(db, f"admin:{username}", "login", "admin", None)
        await db.commit()
    return _redirect("/admin")


@router.post("/admin/logout")
async def logout(request: Request):
    await verify_csrf(request)
    request.session.clear()
    return _redirect("/admin/login")


# -------------------------------------------------------------- dashboard


def _row(a: Appointment, p: Patient) -> dict:
    return {
        "id": str(a.id),
        "token": a.token_label,
        "date": fmt_date(a.start_utc),
        "time": fmt_time(a.start_utc),
        "name": patient_name(p) or "(walk-in)",
        "status": a.status,
        "queue_status": a.queue_status,
        "flag": a.needs_staff_decision,
        "previous": a.previous_token_label,
        "source": a.source,
        "confirmed": bool(a.confirmed_by_patient_at),
    }


@router.get("/admin")
async def dashboard(request: Request, user: str = Depends(admin_user), days: int = 7):
    now = clock.now()
    today = local_today(now)
    async with get_session() as db:
        rows = (
            await db.execute(
                select(Appointment, Patient)
                .join(Patient, Patient.id == Appointment.patient_id)
                .where(
                    Appointment.session_date >= today,
                    Appointment.session_date <= today + timedelta(days=days),
                )
                .order_by(Appointment.start_utc)
            )
        ).all()
    groups: dict[str, list] = {}
    for a, p in rows:
        groups.setdefault(fmt_date(a.start_utc), []).append(_row(a, p))
    return render(request, "dashboard.html", groups=groups, today=today)


@router.post("/admin/appointments/{appt_id}/status")
async def set_status(request: Request, appt_id: uuid.UUID, status: str = Form(...), user: str = Depends(admin_user)):
    """Mark completed / no-show (only from the admin UI, section 11)."""
    await verify_csrf(request)
    actor = f"admin:{user}"
    async with get_session() as db:
        a = await db.get(Appointment, appt_id)
        if a and a.status == "booked" and status in ("completed", "no_show"):
            a.status = status
            if status == "no_show":
                a.queue_status = "no_show"
                p = await db.get(Patient, a.patient_id)
                p.noshow_count += 1
            else:
                a.queue_status = "done"
                a.consult_ended_at = a.consult_ended_at or clock.now()
            await audit(db, actor, f"mark_{status}", "appointment", a.id)
            await db.commit()
    return _redirect(request.headers.get("referer") or "/admin")


@router.post("/admin/appointments/{appt_id}/cancel")
async def staff_cancel(request: Request, appt_id: uuid.UUID, user: str = Depends(admin_user)):
    await verify_csrf(request)
    try:
        await change_service.cancel(appt_id, by="staff", actor=f"admin:{user}", reason_code="staff")
    except DomainError:
        pass
    await outbox.process_outbox()
    return _redirect(request.headers.get("referer") or "/admin")


@router.get("/admin/appointments/{appt_id}/move")
async def move_page(request: Request, appt_id: uuid.UUID, user: str = Depends(admin_user), day: str | None = None):
    async with get_session() as db:
        a = await db.get(Appointment, appt_id)
        p = await db.get(Patient, a.patient_id)
        d = date.fromisoformat(day) if day else max(local_today(clock.now()), a.session_date)
        free = await booking_service.find_slots(db, d, d + timedelta(days=2))
    slots = [{"iso": s.isoformat(), "label": fmt_short(s)} for s in free[:30]]
    return render(request, "move.html", appt=_row(a, p), slots=slots, day=d.isoformat())


@router.post("/admin/appointments/{appt_id}/move")
async def move(request: Request, appt_id: uuid.UUID, new_start: str = Form(...), user: str = Depends(admin_user)):
    await verify_csrf(request)
    try:
        await change_service.reschedule(appt_id, datetime.fromisoformat(new_start), by="staff", actor=f"admin:{user}")
    except DomainError as exc:
        return render(request, "message.html", title="Could not move", message=type(exc).__name__, back="/admin")
    await outbox.process_outbox()
    return _redirect("/admin")


@router.post("/admin/appointments/{appt_id}/clear-flag")
async def clear_flag(request: Request, appt_id: uuid.UUID, user: str = Depends(admin_user)):
    await verify_csrf(request)
    async with get_session() as db:
        a = await db.get(Appointment, appt_id)
        if a:
            await audit(db, f"admin:{user}", "clear_flag", "appointment", a.id, flag=a.needs_staff_decision)
            a.needs_staff_decision = None
            await db.commit()
    return _redirect(request.headers.get("referer") or "/admin")


# -------------------------------------------------------------- follow-ups


@router.post("/admin/appointments/{appt_id}/followup")
async def add_followup(request: Request, appt_id: uuid.UUID, due_date: str = Form(...), note: str = Form("follow-up visit"),
                       user: str = Depends(admin_user)):
    await verify_csrf(request)
    async with get_session() as db:
        a = await db.get(Appointment, appt_id)
        fu = FollowUp(patient_id=a.patient_id, appointment_id=a.id, due_date=date.fromisoformat(due_date),
                      note_for_patient=note[:80])
        db.add(fu)
        await db.flush()
        await audit(db, f"admin:{user}", "followup_add", "follow_up", fu.id)
        await db.commit()
    return _redirect("/admin/followups")


@router.get("/admin/followups")
async def followups(request: Request, user: str = Depends(admin_user)):
    async with get_session() as db:
        rows = (
            await db.execute(
                select(FollowUp, Patient).join(Patient, Patient.id == FollowUp.patient_id).order_by(FollowUp.due_date)
            )
        ).all()
    items = [{"id": str(f.id), "name": patient_name(p), "due": fmt_date(f.due_date), "note": f.note_for_patient,
              "status": f.status} for f, p in rows]
    return render(request, "followups.html", items=items)


@router.post("/admin/followups/{fu_id}/cancel")
async def cancel_followup(request: Request, fu_id: uuid.UUID, user: str = Depends(admin_user)):
    await verify_csrf(request)
    async with get_session() as db:
        fu = await db.get(FollowUp, fu_id)
        if fu and fu.status in ("pending", "sent"):
            fu.status = "cancelled"
            await audit(db, f"admin:{user}", "followup_cancel", "follow_up", fu.id)
            await db.commit()
    return _redirect("/admin/followups")


# ------------------------------------------------------------ handoff inbox


@router.get("/admin/handoff")
async def handoff_inbox(request: Request, user: str = Depends(admin_user)):
    async with get_session() as db:
        rows = (
            await db.execute(
                select(Conversation, Patient)
                .join(Patient, Patient.id == Conversation.patient_id)
                .where(Conversation.handoff_active.is_(True))
                .order_by(Conversation.updated_at.desc())
            )
        ).all()
    items = []
    for c, p in rows:
        r = recipient(p)
        items.append({"id": str(c.id), "name": patient_name(p) or "(no name)", "turns": (c.context_json or {}).get("turns", []),
                      "window_open": r.in_window(), "lang": p.preferred_language})
    return render(request, "handoff.html", items=items)


@router.post("/admin/handoff/{conv_id}/reply")
async def handoff_reply(request: Request, conv_id: uuid.UUID, text: str = Form(...), user: str = Depends(admin_user)):
    await verify_csrf(request)
    async with get_session() as db:
        c = await db.get(Conversation, conv_id)
        p = await db.get(Patient, c.patient_id)
        try:
            await get_wa().send_text(recipient(p), text[:1000])
            turns = list((c.context_json or {}).get("turns", []))
            turns.append({"role": "assistant", "text": f"[staff] {text[:1000]}"})
            c.context_json = {**(c.context_json or {}), "turns": turns[-10:]}
            await audit(db, f"admin:{user}", "handoff_reply", "conversation", c.id)
            await db.commit()
        except SendBlocked as exc:
            return render(request, "message.html", title="Not sent",
                          message=f"{exc}. Outside the 24h window only templates can be sent; please call the patient.",
                          back="/admin/handoff")
    return _redirect("/admin/handoff")


@router.post("/admin/handoff/{conv_id}/return")
async def handoff_return(request: Request, conv_id: uuid.UUID, user: str = Depends(admin_user)):
    await verify_csrf(request)
    async with get_session() as db:
        c = await db.get(Conversation, conv_id)
        c.handoff_active = False
        c.state = S.IDLE
        await audit(db, f"admin:{user}", "handoff_return", "conversation", c.id)
        await db.commit()
    return _redirect("/admin/handoff")


# ---------------------------------------------------------- manual booking


@router.get("/admin/book")
async def book_page(request: Request, user: str = Depends(admin_user), day: str | None = None):
    d = date.fromisoformat(day) if day else local_today(clock.now())
    async with get_session() as db:
        free = await booking_service.find_slots(db, d, d)
    slots = [{"iso": s.isoformat(), "label": fmt_short(s)} for s in free]
    return render(request, "book.html", slots=slots, day=d.isoformat(), error=None)


@router.post("/admin/book")
async def book_submit(request: Request, phone: str = Form(...), name: str = Form(...), start: str = Form(...),
                      user: str = Depends(admin_user)):
    await verify_csrf(request)
    async with get_session() as db:
        p, _ = await get_or_create_patient(db, phone)
        p.name_enc = encrypt(name.strip()[:80])
        if p.consent_given_at is None:
            p.consent_given_at = clock.now()  # verbal consent taken at the desk / on the phone
            p.consent_version = "staff-verbal"
        await db.commit()
        pid = p.id
    try:
        await booking_service.book(pid, datetime.fromisoformat(start), source="admin", actor=f"admin:{user}",
                                   enforce_limit=False)
    except DomainError as exc:
        return render(request, "message.html", title="Booking failed", message=type(exc).__name__, back="/admin/book")
    await outbox.process_outbox()
    return _redirect("/admin")


# ------------------------------------------------------ clinic-side changes


@router.get("/admin/sessions")
async def sessions_page(request: Request, user: str = Depends(admin_user), day: str | None = None):
    d = date.fromisoformat(day) if day else local_today(clock.now())
    sessions = [w.name for w in session_windows(get_clinic(), d, include_holidays=True)]
    return render(request, "sessions.html", day=d.isoformat(), sessions=sessions, affected=None)


@router.post("/admin/sessions/cancel")
async def cancel_session(request: Request, day: str = Form(...), session: str = Form(...), user: str = Depends(admin_user)):
    await verify_csrf(request)
    n = await change_service.cancel_session_by_clinic(date.fromisoformat(day), session, f"admin:{user}")
    await outbox.process_outbox()
    return render(request, "message.html", title="Session cancelled",
                  message=f"{n} appointment(s) cancelled and patients notified.", back="/admin")


@router.post("/admin/sessions/block")
async def block_time(request: Request, day: str = Form(...), start: str = Form(...), end: str = Form(...),
                     user: str = Depends(admin_user)):
    """Block a period: adds a busy event; affected bookings are listed for a staff decision (never auto-cancelled)."""
    await verify_csrf(request)
    d = date.fromisoformat(day)
    s_utc = local_to_utc(d, time.fromisoformat(start))
    e_utc = local_to_utc(d, time.fromisoformat(end))
    cal = get_calendar()
    await cal.add_busy_block(s_utc, e_utc, "Blocked by clinic")
    affected = []
    async with get_session() as db:
        rows = (
            await db.execute(
                select(Appointment, Patient).join(Patient, Patient.id == Appointment.patient_id).where(
                    Appointment.status == "booked", Appointment.start_utc < e_utc, Appointment.end_utc > s_utc,
                    Appointment.is_walk_in.is_(False),
                )
            )
        ).all()
        free = await booking_service.find_slots(db, d, d + timedelta(days=1))
        for a, p in rows:
            a.needs_staff_decision = "blocked_time"
            nearest = sorted(free, key=lambda s: abs((s - a.start_utc).total_seconds()))[:3]
            affected.append({**_row(a, p), "suggestions": [{"iso": s.isoformat(), "label": fmt_short(s)} for s in nearest]})
        await audit(db, f"admin:{user}", "block_time", "session", f"{day}", start=start, end=end, affected=len(rows))
        await db.commit()
    sessions = [w.name for w in session_windows(get_clinic(), d, include_holidays=True)]
    return render(request, "sessions.html", day=day, sessions=sessions, affected=affected)


@router.get("/admin/audit")
async def audit_page(request: Request, user: str = Depends(admin_user)):
    async with get_session() as db:
        rows = (await db.scalars(select(AuditLog).order_by(AuditLog.ts.desc()).limit(200))).all()
    return render(request, "audit.html", rows=rows)
