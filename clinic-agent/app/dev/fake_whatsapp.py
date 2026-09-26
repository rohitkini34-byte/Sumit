"""WA_MODE=mock transport: keeps outbound messages in memory for the /dev/chat simulator and tests."""
from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from app.dev import clock
from app.whatsapp.client import Transport


@dataclass
class SentMessage:
    id: str
    to: str
    kind: str  # text | buttons | list | template
    text: str
    buttons: list = field(default_factory=list)  # [(id, title)]
    rows: list = field(default_factory=list)  # [(id, title, desc)]
    template: str | None = None
    payload: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=clock.now)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "to": self.to,
            "kind": self.kind,
            "text": self.text,
            "buttons": [list(b) for b in self.buttons],
            "rows": [list(r) for r in self.rows],
            "template": self.template,
            "ts": self.ts.isoformat(),
        }


class FakeTransport(Transport):
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.read: list[str] = []
        self._seq = itertools.count(1)
        self.fail_next = 0

    async def post(self, payload: dict, meta: dict) -> str:
        if self.fail_next:
            self.fail_next -= 1
            from app.whatsapp.client import SendFailed

            raise SendFailed("simulated failure status=500")
        msg_id = f"wamid.fake.{next(self._seq)}"
        self.sent.append(
            SentMessage(
                id=msg_id,
                to=meta["to"],
                kind=meta.get("kind", "text"),
                text=meta.get("text", ""),
                buttons=meta.get("buttons", []),
                rows=meta.get("rows", []),
                template=meta.get("template"),
                payload=payload,
            )
        )
        return msg_id

    async def mark_as_read(self, wa_message_id: str) -> None:
        self.read.append(wa_message_id)

    # helpers for tests / simulator
    def for_phone(self, phone: str) -> list[SentMessage]:
        return [m for m in self.sent if m.to == phone]

    def by_phone(self) -> dict[str, list[SentMessage]]:
        out: dict[str, list[SentMessage]] = defaultdict(list)
        for m in self.sent:
            out[m.to].append(m)
        return out

    def clear(self) -> None:
        self.sent.clear()
