"""Transactional outbox: WhatsApp messages and calendar calls happen after commit, with retries."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutboxMessage
from app.db.session import get_session
from app.dev import clock

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 6


def enqueue(db: AsyncSession, kind: str, payload: dict, dedupe_key: str | None = None) -> None:
    """Add to the current transaction. Duplicate dedupe keys are dropped at flush time via
    a pre-check (callers use deterministic keys for things that must be sent once)."""
    db.add(
        OutboxMessage(
            kind=kind,
            payload_json=payload,
            dedupe_key=dedupe_key or f"auto:{uuid.uuid4().hex}",
            next_attempt_at=clock.now(),
        )
    )


async def enqueue_once(db: AsyncSession, kind: str, payload: dict, dedupe_key: str) -> bool:
    exists = await db.scalar(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == dedupe_key))
    if exists:
        return False
    enqueue(db, kind, payload, dedupe_key)
    return True


async def notify(db: AsyncSession, patient_id, template: str, dedupe_key: str | None = None, **data) -> bool:
    """Queue a patient notification. `template` is a template name from whatsapp/templates.py,
    or "text" (free-form i18n key, dropped if outside the 24h window)."""
    payload = {"patient_id": str(patient_id), "template": template, **_jsonable(data)}
    if dedupe_key:
        return await enqueue_once(db, "whatsapp", payload, dedupe_key)
    enqueue(db, "whatsapp", payload)
    return True


def notify_staff(db: AsyncSession, key: str, **data) -> None:
    enqueue(db, "whatsapp", {"template": "staff", "key": key, **_jsonable(data)})


async def gcal(db: AsyncSession, op: str, dedupe_key: str | None = None, **data) -> None:
    payload = {"op": op, **_jsonable(data)}
    if dedupe_key:
        await enqueue_once(db, "gcal", payload, dedupe_key)
    else:
        enqueue(db, "gcal", payload)


# Set to False in tests so the outbox is processed explicitly.
AUTO_KICK = True
_pending_kick = None


def kick() -> None:
    """Process the outbox soon, in the background (called after a commit)."""
    global _pending_kick
    if not AUTO_KICK:
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _pending_kick is None or _pending_kick.done():
        _pending_kick = loop.create_task(_kick_soon())


async def _kick_soon() -> None:
    import asyncio

    await asyncio.sleep(0.05)
    try:
        await process_outbox()
    except Exception:  # pragma: no cover - logged by caller loop
        log.exception("outbox processing failed")


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v
    return out


async def process_outbox(limit: int = 100) -> int:
    """Process due pending rows. Safe to run from several workers (rows are claimed)."""
    from app.scheduling.notifications import deliver_gcal, deliver_whatsapp

    now = clock.now()
    async with get_session() as db:
        ids = (
            await db.scalars(
                select(OutboxMessage.id)
                .where(OutboxMessage.status == "pending", OutboxMessage.next_attempt_at <= now)
                .order_by(OutboxMessage.created_at)
                .limit(limit)
            )
        ).all()
    done = 0
    for oid in ids:
        async with get_session() as db:
            claimed = await db.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == oid, OutboxMessage.status == "pending")
                .values(status="processing")
            )
            await db.commit()
            if claimed.rowcount != 1:
                continue
            row = await db.get(OutboxMessage, oid)
            try:
                if row.kind == "whatsapp":
                    await deliver_whatsapp(db, row.payload_json)
                else:
                    await deliver_gcal(db, row.payload_json)
                row.status = "done"
                done += 1
            except Exception as exc:  # retry with backoff
                row.attempts += 1
                row.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = "failed"
                    log.error("outbox %s failed permanently: %s", row.id, row.last_error)
                else:
                    row.status = "pending"
                    row.next_attempt_at = clock.now() + timedelta(minutes=2 ** (row.attempts - 1))
                    log.warning("outbox %s attempt %s failed: %s", row.id, row.attempts, row.last_error)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
    return done
