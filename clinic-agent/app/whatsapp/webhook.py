"""GET verify + POST receive (section 7.1)."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ReminderLog
from app.db.session import get_session
from app.ratelimit import RateLimiter
from app.whatsapp.parser import StatusUpdate, parse_webhook
from app.whatsapp.signature import verify_signature

log = logging.getLogger(__name__)
router = APIRouter()
_limiter = RateLimiter(per_minute=600)


@router.get("/webhook/whatsapp")
async def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == get_settings().WA_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403)


@router.post("/webhook/whatsapp")
async def receive(request: Request, background: BackgroundTasks):
    if not _limiter.allow(request.client.host if request.client else "?"):
        raise HTTPException(status_code=429)
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), get_settings().WA_APP_SECRET):
        raise HTTPException(status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400)
    messages, statuses = parse_webhook(payload)
    background.add_task(process_payload, messages, statuses)
    return {"ok": True}


async def process_payload(messages, statuses) -> None:
    from app.agent.router import handle_inbound

    for st in statuses:
        await _update_status(st)
    for m in messages:
        try:
            await handle_inbound(m)
        except Exception:
            log.exception("failed to process inbound message %s", m.wa_message_id[-8:])


async def _update_status(st: StatusUpdate) -> None:
    if not st.wa_message_id:
        return
    async with get_session() as db:
        row = await db.scalar(select(ReminderLog).where(ReminderLog.wa_message_id == st.wa_message_id))
        if row and st.status in ("sent", "delivered", "read", "failed"):
            row.status = st.status
            await db.commit()
