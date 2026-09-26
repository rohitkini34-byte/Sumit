"""Shared helpers: patients, audit log, queue events."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crypto import decrypt, encrypt, normalize_phone, phone_hash
from app.db.models import Appointment, AuditLog, Patient, QueueEvent
from app.dev import clock
from app.whatsapp.client import Recipient


class DomainError(Exception):
    """Base for expected business-rule failures. `code` maps to an i18n key."""

    code = "book.failed"

    def __init__(self, msg: str = "", **ctx):
        super().__init__(msg or self.code)
        self.ctx = ctx


async def get_or_create_patient(db: AsyncSession, phone: str) -> tuple[Patient, bool]:
    e164 = normalize_phone(phone)
    h = phone_hash(e164)
    p = await db.scalar(select(Patient).where(Patient.phone_hash == h))
    if p:
        return p, False
    p = Patient(phone_hash=h, phone_enc=encrypt(e164))
    db.add(p)
    await db.flush()
    return p, True


def patient_phone(p: Patient) -> str | None:
    return decrypt(p.phone_enc)


def patient_name(p: Patient) -> str:
    return decrypt(p.name_enc) or ""


def first_name(p: Patient) -> str:
    n = patient_name(p).strip()
    return n.split()[0] if n else ""


def recipient(p: Patient) -> Recipient:
    return Recipient(
        phone=patient_phone(p) or "",
        lang=p.preferred_language or "en",
        last_inbound_at=p.last_inbound_at,
        opted_out=p.opted_out,
    )


def calendar_summary(name: str) -> str:
    parts = name.split()
    if not parts:
        return "Appt – Patient"
    if len(parts) == 1:
        return f"Appt – {parts[0]}"
    return f"Appt – {parts[0]} {parts[-1][0]}."


def short_ref(appt_id: uuid.UUID) -> str:
    return appt_id.hex[:8].upper()


async def audit(db: AsyncSession, actor: str, action: str, entity: str, entity_id=None, **meta) -> None:
    db.add(
        AuditLog(actor=actor, action=action, entity=entity, entity_id=str(entity_id) if entity_id else None, meta_json=meta)
    )


async def qevent(db: AsyncSession, appt: Appointment, event: str, actor: str, **meta) -> None:
    db.add(QueueEvent(appointment_id=appt.id, event=event, ts=clock.now(), actor=actor, meta_json=meta))
