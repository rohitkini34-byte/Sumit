import re
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db.models import Appointment, AuditLog, Conversation, FollowUp
from app.dev import clock
from tests.conftest import book, get_appt, ist, make_patient, rows


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
async def anon():
    from app.main import create_app

    app = create_app(start_scheduler=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False) as c:
        yield c


@pytest.fixture
async def admin(anon):
    r = await anon.get("/admin/login")
    tok = csrf_of(r.text)
    r = await anon.post("/admin/login", data={"username": "admin", "password": "admin", "csrf": tok})
    assert r.status_code == 303
    anon.csrf = csrf_of((await anon.get("/admin")).text)  # the token rotates at login
    assert anon.csrf != tok
    return anon


async def test_login_required_and_csrf(anon):
    r = await anon.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    r = await anon.post("/admin/login", data={"username": "admin", "password": "admin"})
    assert r.status_code == 403  # no CSRF token
    r = await anon.get("/admin/login")
    r = await anon.post("/admin/login", data={"username": "admin", "password": "wrong", "csrf": csrf_of(r.text)})
    assert "Wrong username" in r.text


async def test_login_rate_limited(anon):
    r = await anon.get("/admin/login")
    tok = csrf_of(r.text)
    for _ in range(10):
        await anon.post("/admin/login", data={"username": "admin", "password": "x", "csrf": tok})
    r = await anon.post("/admin/login", data={"username": "admin", "password": "admin", "csrf": tok})
    assert "Too many attempts" in r.text


async def test_dashboard_and_actions(admin):
    p = await make_patient()
    a = await book(p, "2026-09-28 10:00")
    r = await admin.get("/admin")
    assert r.status_code == 200 and "M-01" in r.text and "Asha Patil" in r.text
    r = await admin.post(f"/admin/appointments/{a.id}/followup", data={"csrf": admin.csrf, "due_date": "2026-10-10", "note": "follow-up visit"})
    assert r.status_code == 303 and len(await rows(FollowUp)) == 1
    r = await admin.post(f"/admin/appointments/{a.id}/status", data={"csrf": admin.csrf, "status": "completed"})
    assert (await get_appt(a.id)).status == "completed"
    assert any(x.actor == "admin:admin" and x.action == "mark_completed" for x in await rows(AuditLog))
    for page in ("/admin/followups", "/admin/handoff", "/admin/book", "/admin/sessions", "/admin/audit"):
        assert (await admin.get(page)).status_code == 200, page


async def test_post_without_csrf_rejected(admin):
    p = await make_patient()
    a = await book(p, "2026-09-28 10:00")
    r = await admin.post(f"/admin/appointments/{a.id}/cancel", data={})
    assert r.status_code == 403 and (await get_appt(a.id)).status == "booked"


async def test_manual_booking_and_move(admin):
    r = await admin.post("/admin/book", data={"csrf": admin.csrf, "phone": "+919812345678", "name": "Desk Patient",
                                              "start": ist("2026-09-28 11:00").isoformat()})
    assert r.status_code == 303
    a = (await rows(Appointment))[0]
    assert a.source == "admin" and a.token_label == "M-05"
    r = await admin.post(f"/admin/appointments/{a.id}/move", data={"csrf": admin.csrf, "new_start": ist("2026-09-28 11:30").isoformat()})
    assert r.status_code == 303
    assert (await get_appt(a.id)).status == "rescheduled"


async def test_queue_board_flow_and_display_has_no_names(admin):
    p1 = await make_patient("+919800000001", "Asha Patil")
    p2 = await make_patient("+919800000002", "Ravi Kumar")
    a1 = await book(p1, "2026-09-28 10:00")
    a2 = await book(p2, "2026-09-28 10:15")
    clock.set_now(ist("2026-09-28 09:55"))
    base = {"csrf": admin.csrf, "day": "2026-09-28", "session": "morning"}
    await admin.post("/admin/queue/action", data={**base, "action": "check_in", "appt_id": str(a1.id)})
    await admin.post("/admin/queue/action", data={**base, "action": "check_in", "appt_id": str(a2.id)})
    clock.set_now(ist("2026-09-28 10:00"))
    r = await admin.post("/admin/queue/action", data={**base, "action": "call_next", "expected_current": "none"},
                         headers={"HX-Request": "true", "X-CSRF-Token": admin.csrf})
    assert r.status_code == 200 and "Now with doctor: <strong>M-01</strong>" in r.text
    # a stale second tap (board still showed nobody with the doctor) changes nothing
    await admin.post("/admin/queue/action", data={**base, "action": "call_next", "expected_current": "none"})
    assert (await get_appt(a2.id)).queue_status == "checked_in"
    await admin.post("/admin/queue/action", data={**base, "action": "walk_in", "name": "Walk In"})
    r = await admin.get("/admin/queue?day=2026-09-28&session=morning")
    assert "W-01" in r.text and "Asha" in r.text
    r = await admin.get("/admin/queue/board?day=2026-09-28&session=morning")
    assert r.status_code == 200
    key = get_settings().QUEUE_DISPLAY_KEY
    d = await admin.get(f"/queue/display/{key}?session=morning")
    assert d.status_code == 200 and "M-01" in d.text and "M-02" in d.text
    assert "Asha" not in d.text and "Ravi" not in d.text and "Walk In" not in d.text
    assert (await admin.get("/queue/display/wrong-key")).status_code == 404


async def test_handoff_reply_and_return(admin, wa):
    from tests.conftest import send

    await make_patient()
    await send("+919800000001", "talk to a real person please")
    conv = (await rows(Conversation))[0]
    assert conv.handoff_active
    r = await admin.get("/admin/handoff")
    assert "talk to a real person" in r.text
    await admin.post(f"/admin/handoff/{conv.id}/reply", data={"csrf": admin.csrf, "text": "Hello from reception"})
    assert wa.for_phone("+919800000001")[-1].text == "Hello from reception"
    await admin.post(f"/admin/handoff/{conv.id}/return", data={"csrf": admin.csrf})
    assert not (await rows(Conversation))[0].handoff_active


async def test_block_time_flags_but_never_cancels(admin):
    p = await make_patient()
    a = await book(p, "2026-09-28 11:00")
    r = await admin.post("/admin/sessions/block", data={"csrf": admin.csrf, "day": "2026-09-28", "start": "10:45", "end": "11:30"})
    assert r.status_code == 200 and "M-05" in r.text
    got = await get_appt(a.id)
    assert got.status == "booked" and got.needs_staff_decision == "blocked_time"


async def test_session_cancel_from_admin(admin, wa):
    p = await make_patient()
    await book(p, "2026-09-28 11:00")
    r = await admin.post("/admin/sessions/cancel", data={"csrf": admin.csrf, "day": "2026-09-28", "session": "morning"})
    assert "1 appointment" in r.text


async def test_dev_routes_only_in_dev(monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "APP_ENV", "staging")
    app = create_app(start_scheduler=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/dev/chat")).status_code == 404
        assert (await c.get("/dev/clock")).status_code == 404


async def test_dev_tools(anon, wa):
    assert (await anon.get("/dev")).status_code == 200
    assert "WA_APP_SECRET" not in (await anon.get("/dev/chat")).text
    r = await anon.post("/dev/clock", data={"action": "set", "when": "2026-09-27T10:05"})
    assert r.status_code == 303
    r = await anon.post("/dev/run-jobs", headers={"accept": "application/json"})
    assert r.status_code == 200 and "reminders" in r.json()
    r = await anon.post("/dev/calendar/busy", data={"day": "2026-09-29", "start": "10:00", "end": "11:00"})
    assert r.status_code == 303
    assert "busy block" in (await anon.get("/dev/calendar")).text
    assert (await anon.get("/health")).json()["status"] == "ok"


async def test_queue_simulator(anon):
    r = await anon.post("/dev/queue-sim", data={"day": "2026-09-29", "session": "morning", "consult": "15",
                                                "arrivals": "ontime\nlate:15\nontime\nnoshow\nearly:20\ncancel"})
    assert r.status_code == 200 and "Result" in r.text
    assert "no_show" in r.text and "reinserted" in r.text or "skipped" in r.text
