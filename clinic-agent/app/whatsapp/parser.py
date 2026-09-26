"""Normalize Meta webhook payloads into InboundMessage / StatusUpdate objects."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.crypto import normalize_phone

SUPPORTED = {"text", "interactive", "button"}


@dataclass
class InboundMessage:
    wa_message_id: str
    from_phone: str  # E.164 with '+'
    timestamp: datetime
    type: str  # text | interactive | button | <unsupported type>
    text: str | None = None
    reply_id: str | None = None  # list/button reply id or template button payload
    reply_title: str | None = None
    profile_name: str | None = None

    @property
    def supported(self) -> bool:
        return self.type in SUPPORTED and (self.text is not None or self.reply_id is not None)

    @property
    def is_structured(self) -> bool:
        return self.reply_id is not None


@dataclass
class StatusUpdate:
    wa_message_id: str
    status: str  # sent | delivered | read | failed
    recipient: str | None = None


def parse_webhook(payload: dict) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", []) or []
            }
            for m in value.get("messages", []) or []:
                messages.append(_parse_message(m, names))
            for s in value.get("statuses", []) or []:
                statuses.append(StatusUpdate(s.get("id", ""), s.get("status", ""), s.get("recipient_id")))
    return messages, statuses


def _parse_message(m: dict, names: dict) -> InboundMessage:
    mtype = m.get("type", "unknown")
    ts = datetime.fromtimestamp(int(m.get("timestamp", "0") or 0), tz=timezone.utc)
    msg = InboundMessage(
        wa_message_id=m.get("id", ""),
        from_phone=normalize_phone(m.get("from", "")),
        timestamp=ts,
        type=mtype,
        profile_name=names.get(m.get("from")),
    )
    if mtype == "text":
        msg.text = (m.get("text") or {}).get("body", "")
    elif mtype == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("list_reply") or inter.get("button_reply") or {}
        msg.reply_id = reply.get("id")
        msg.reply_title = reply.get("title")
    elif mtype == "button":  # quick reply on a template
        btn = m.get("button") or {}
        msg.reply_id = btn.get("payload")
        msg.reply_title = btn.get("text")
    return msg
