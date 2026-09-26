"""Conversation state machine (section 9) and the per-message context object."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.models import Conversation, Patient
from app.db.session import get_session
from app.dev import clock
from app.scheduling.common import recipient
from app.whatsapp.client import Recipient

NEW = "NEW"
AWAITING_CONSENT = "AWAITING_CONSENT"
IDLE = "IDLE"
CHOOSING_SLOT = "CHOOSING_SLOT"
ASKING_NAME = "ASKING_NAME"
CONFIRMING_BOOKING = "CONFIRMING_BOOKING"
CHOOSING_CANCEL = "CHOOSING_CANCEL"
CONFIRMING_CANCEL = "CONFIRMING_CANCEL"
RESCHEDULE_CHOOSING_SLOT = "RESCHEDULE_CHOOSING_SLOT"
CONFIRMING_RESCHEDULE = "CONFIRMING_RESCHEDULE"
HANDOFF = "HANDOFF"

MAX_TURNS = 10
DRAFT_TTL = timedelta(minutes=10)


@dataclass
class Ctx:
    """Snapshot of the patient + conversation for one inbound message.

    Uses short-lived DB sessions only (never holds a transaction open while services run)."""

    patient: Patient
    conv_id: uuid.UUID
    state: str
    context: dict
    handoff: bool
    sent: list = field(default_factory=list)  # texts the bot sent during this message (for turns)

    @property
    def lang(self) -> str:
        return self.patient.preferred_language or "en"

    @property
    def r(self) -> Recipient:
        return recipient(self.patient)

    # ------------------------------------------------------------ persistence
    @classmethod
    async def load(cls, patient_id: uuid.UUID) -> "Ctx":
        async with get_session() as db:
            patient = await db.get(Patient, patient_id)
            conv = await db.scalar(select(Conversation).where(Conversation.patient_id == patient_id))
            if conv is None:
                conv = Conversation(patient_id=patient_id, state=NEW, context_json={})
                db.add(conv)
                await db.commit()
            return cls(patient, conv.id, conv.state, dict(conv.context_json or {}), conv.handoff_active)

    async def save(self) -> None:
        async with get_session() as db:
            conv = await db.get(Conversation, self.conv_id)
            conv.state = self.state
            conv.context_json = self.context
            conv.handoff_active = self.handoff
            conv.updated_at = clock.now()
            await db.commit()

    async def update_patient(self, **fields) -> None:
        async with get_session() as db:
            p = await db.get(Patient, self.patient.id)
            for k, v in fields.items():
                setattr(p, k, v)
            await db.commit()
            self.patient = p

    async def refresh_patient(self) -> None:
        async with get_session() as db:
            self.patient = await db.get(Patient, self.patient.id)

    # ------------------------------------------------------------------ turns
    def add_turn(self, role: str, text: str) -> None:
        if not text:
            return
        turns = self.context.setdefault("turns", [])
        turns.append({"role": role, "text": text[:1000]})
        del turns[:-MAX_TURNS]

    def turns(self) -> list[dict]:
        return list(self.context.get("turns", []))

    # ------------------------------------------------------------------ drafts
    def new_draft(self, kind: str, start_utc: datetime, **data) -> dict:
        draft = {
            "id": uuid.uuid4().hex[:10],
            "kind": kind,
            "start": start_utc.isoformat(),
            "expires": (clock.now() + DRAFT_TTL).isoformat(),
            **data,
        }
        self.context["draft"] = draft
        return draft

    def get_draft(self, draft_id: str) -> tuple[dict | None, bool]:
        """Returns (draft, expired)."""
        d = self.context.get("draft")
        if not d or d.get("id") != draft_id:
            return None, False
        return d, datetime.fromisoformat(d["expires"]) < clock.now()

    def clear_draft(self) -> None:
        self.context.pop("draft", None)
