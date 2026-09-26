"""Test fixtures. Runs fully offline: mock WhatsApp, fake calendar, mock LLM, frozen clock.

DB: a fresh SQLite file per test by default; set TEST_DATABASE_URL=postgresql+asyncpg://...
to run the same suite on Postgres (advisory locks, partial indexes).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

os.environ.update(
    APP_ENV="dev",
    WA_MODE="mock",
    CALENDAR_MODE="fake",
    LLM_MODE="mock",
    ENABLE_SCHEDULER="false",
    WA_APP_SECRET="test-app-secret",
    WA_VERIFY_TOKEN="test-verify-token",
    STAFF_WHATSAPP_NUMBERS="+919999999999",
    DEV_ALLOWED_NUMBERS="",
)
os.environ.pop("DATABASE_URL", None)

import pytest  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.config import get_settings, set_clinic_override  # noqa: E402
from app.db import session as dbsession  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.dev import clock  # noqa: E402

TZ = get_settings().tz
PG_URL = os.environ.get("TEST_DATABASE_URL")


def ist(s: str) -> datetime:
    """'2026-09-28 10:00' in clinic time -> aware datetime."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


@pytest.fixture(autouse=True)
async def env(tmp_path):
    from app.agent.llm import set_llm
    from app.agent.mock_llm import MockLLM
    from app.admin.auth import login_limiter
    from app.calendar.base import set_calendar
    from app.dev.fake_calendar import FakeCalendar
    from app.dev.fake_whatsapp import FakeTransport
    from app.scheduling import offers, outbox
    from app.whatsapp.client import WhatsAppClient, set_wa

    url = PG_URL or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    dbsession.init_engine(url)
    async with dbsession.engine().begin() as conn:
        if PG_URL:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    clock.set_now(ist("2026-09-28 07:00"))  # Monday, before the morning session
    set_wa(WhatsAppClient(FakeTransport()))
    set_calendar(FakeCalendar())
    set_llm(MockLLM())
    set_clinic_override(None)
    outbox.AUTO_KICK = False
    offers.expiry_timer_hook = None
    login_limiter.reset()
    yield
    clock.reset()
    set_clinic_override(None)
    await dbsession.dispose()


@pytest.fixture
def wa():
    from app.whatsapp.client import get_wa

    return get_wa().transport


@pytest.fixture
def cal():
    from app.calendar.base import get_calendar

    return get_calendar()


@pytest.fixture
def llm():
    from app.agent.llm import get_llm

    return get_llm()


def tune(**changes):
    """Override clinic config values for one test: tune(queue={'grace_minutes': 5})."""
    from app.config import get_clinic

    cfg = get_clinic().model_copy(deep=True)
    for section, values in changes.items():
        if isinstance(values, dict):
            obj = getattr(cfg, section)
            for k, v in values.items():
                if isinstance(v, dict):
                    sub = getattr(obj, k)
                    for kk, vv in v.items():
                        setattr(sub, kk, vv)
                else:
                    setattr(obj, k, v)
        else:
            setattr(cfg, section, values)
    set_clinic_override(cfg)
    return cfg


async def make_patient(phone: str = "+919800000001", name: str | None = "Asha Patil", *, consent=True, lang="en",
                       inbound=True, opted_out=False):
    from app.db.crypto import encrypt
    from app.scheduling.common import get_or_create_patient

    async with dbsession.get_session() as db:
        p, _ = await get_or_create_patient(db, phone)
        if name:
            p.name_enc = encrypt(name)
        p.preferred_language = lang
        p.opted_out = opted_out
        if consent:
            p.consent_given_at = clock.now()
            p.consent_version = "test"
        if inbound:
            p.last_inbound_at = clock.now()
        await db.commit()
        return p


async def book(patient, when: str, **kw):
    from app.scheduling import booking_service

    return await booking_service.book(patient.id, ist(when), enforce_limit=kw.pop("enforce_limit", False), **kw)


async def get_appt(appt_id):
    from app.db.models import Appointment

    async with dbsession.get_session() as db:
        return await db.get(Appointment, appt_id)


async def get_patient(pid):
    from app.db.models import Patient

    async with dbsession.get_session() as db:
        return await db.get(Patient, pid)


async def rows(model, *where):
    async with dbsession.get_session() as db:
        return list((await db.scalars(select(model).where(*where))).all())


async def flush_outbox():
    from app.scheduling import outbox

    return await outbox.process_outbox()


def _msg(phone: str, **kw):
    from app.whatsapp.parser import InboundMessage

    return InboundMessage(wa_message_id=f"wamid.test.{uuid.uuid4().hex}", from_phone=phone, timestamp=clock.now(), **kw)


async def send(phone: str, text: str):
    from app.agent.router import handle_inbound

    await handle_inbound(_msg(phone, type="text", text=text))


async def tap(phone: str, reply_id: str, title: str = "tap", kind: str = "interactive"):
    from app.agent.router import handle_inbound

    await handle_inbound(_msg(phone, type=kind, reply_id=reply_id, reply_title=title))


def last(wa, phone: str):
    msgs = wa.for_phone(phone)
    return msgs[-1] if msgs else None


def find_button(wa, phone: str, prefix: str) -> str:
    for m in reversed(wa.for_phone(phone)):
        for bid, _title in m.buttons:
            if bid.startswith(prefix):
                return bid
        for rid, _t, _d in m.rows:
            if rid.startswith(prefix):
                return rid
    raise AssertionError(f"no button/row starting with {prefix!r} sent to {phone}")
