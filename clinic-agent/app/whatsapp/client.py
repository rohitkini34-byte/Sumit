"""WhatsApp Cloud API sender.

Every guard lives here so no caller can bypass it:
- opted-out patients get nothing except the opt-out acknowledgement,
- free-form messages only inside the 24h customer-service window (templates otherwise),
- in dev/staging, only numbers in DEV_ALLOWED_NUMBERS are messaged when WA_MODE=live,
- interactive list / button limits are enforced.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from app.config import get_settings
from app.db.crypto import mask_phone
from app.dev import clock
from app.whatsapp import templates as tpl

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
MAX_LIST_ROWS = 10
ROW_TITLE = 24
ROW_DESC = 72
LIST_BUTTON = 20
MAX_BUTTONS = 3
BUTTON_TITLE = 20
BODY_MAX = 1024


class SendBlocked(Exception):
    """A guard refused the send (opt-out, window, allow-list)."""


class OutsideWindow(SendBlocked):
    pass


class SendFailed(Exception):
    pass


@dataclass
class Recipient:
    phone: str  # E.164
    lang: str = "en"
    last_inbound_at: datetime | None = None
    opted_out: bool = False
    is_staff: bool = False

    def in_window(self) -> bool:
        if self.is_staff:
            return True
        return self.last_inbound_at is not None and clock.now() - self.last_inbound_at < WINDOW


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class Transport:
    async def post(self, payload: dict, meta: dict) -> str:  # returns wa message id
        raise NotImplementedError

    async def mark_as_read(self, wa_message_id: str) -> None:
        raise NotImplementedError


class LiveTransport(Transport):
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http

    def _url(self, path: str = "messages") -> str:
        s = get_settings()
        return f"https://graph.facebook.com/{s.WA_GRAPH_API_VERSION}/{s.WA_PHONE_NUMBER_ID}/{path}"

    async def _request(self, body: dict) -> dict:
        s = get_settings()
        headers = {"Authorization": f"Bearer {s.WA_ACCESS_TOKEN}"}
        client = self._http or httpx.AsyncClient(timeout=15)
        try:
            delay = 1.0
            for attempt in range(1, 4):
                try:
                    resp = await client.post(self._url(), json=body, headers=headers)
                except httpx.TransportError as exc:
                    if attempt == 3:
                        raise SendFailed(f"transport error: {type(exc).__name__}") from exc
                else:
                    if resp.status_code < 300:
                        return resp.json()
                    if resp.status_code != 429 and resp.status_code < 500:
                        err = {}
                        try:
                            err = resp.json().get("error", {})
                        except ValueError:
                            pass
                        raise SendFailed(f"status={resp.status_code} code={err.get('code')}")
                    if attempt == 3:
                        raise SendFailed(f"status={resp.status_code} after retries")
                await asyncio.sleep(delay)
                delay *= 2
        finally:
            if self._http is None:
                await client.aclose()
        raise SendFailed("unreachable")

    async def post(self, payload: dict, meta: dict) -> str:
        s = get_settings()
        try:
            data = await self._request(payload)
        except SendFailed as exc:
            # Staging only: templates not yet approved -> fall back to hello_world.
            if payload.get("type") == "template" and s.APP_ENV == "staging" and "code=132001" in str(exc):
                log.warning("template %s not approved yet; sending hello_world", meta.get("template"))
                fallback = {
                    "messaging_product": "whatsapp",
                    "to": payload["to"],
                    "type": "template",
                    "template": {"name": "hello_world", "language": {"code": "en_US"}},
                }
                data = await self._request(fallback)
            else:
                raise
        return (data.get("messages") or [{}])[0].get("id", "")

    async def mark_as_read(self, wa_message_id: str) -> None:
        await self._request({"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id})


class WhatsAppClient:
    def __init__(self, transport: Transport):
        self.transport = transport

    # ------------------------------------------------------------------ guards
    def _guard(self, r: Recipient, *, free_form: bool, allow_opted_out: bool = False) -> None:
        s = get_settings()
        if r.opted_out and not allow_opted_out:
            raise SendBlocked("recipient opted out")
        if free_form and not r.in_window():
            raise OutsideWindow("outside 24h window: use a template")
        if s.WA_MODE == "live" and s.APP_ENV in ("dev", "staging") and r.phone not in s.allowed_numbers:
            raise SendBlocked("number not in DEV_ALLOWED_NUMBERS")

    async def _send(self, r: Recipient, payload: dict, meta: dict) -> str:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": r.phone.lstrip("+"), **payload}
        try:
            msg_id = await self.transport.post(payload, {**meta, "to": r.phone})
        except SendFailed as exc:
            log.error("whatsapp send failed to=%s kind=%s err=%s", mask_phone(r.phone), meta.get("kind"), exc)
            raise
        log.info("whatsapp sent to=%s kind=%s", mask_phone(r.phone), meta.get("kind"))
        return msg_id

    # ------------------------------------------------------------------ senders
    async def send_text(self, r: Recipient, text: str, *, allow_opted_out: bool = False) -> str:
        self._guard(r, free_form=True, allow_opted_out=allow_opted_out)
        return await self._send(
            r, {"type": "text", "text": {"body": text[:4096], "preview_url": False}}, {"kind": "text", "text": text}
        )

    async def send_buttons(self, r: Recipient, body: str, buttons: list[tuple[str, str]]) -> str:
        self._guard(r, free_form=True)
        if not 1 <= len(buttons) <= MAX_BUTTONS:
            raise ValueError("1..3 reply buttons allowed")
        btns = [{"type": "reply", "reply": {"id": bid[:256], "title": _clip(title, BUTTON_TITLE)}} for bid, title in buttons]
        payload = {"type": "interactive", "interactive": {"type": "button", "body": {"text": body[:BODY_MAX]}, "action": {"buttons": btns}}}
        return await self._send(r, payload, {"kind": "buttons", "text": body, "buttons": [(b["reply"]["id"], b["reply"]["title"]) for b in btns]})

    async def send_list(
        self, r: Recipient, body: str, button_label: str, rows: list[tuple[str, str, str]], section_title: str = ""
    ) -> str:
        self._guard(r, free_form=True)
        if not 1 <= len(rows) <= MAX_LIST_ROWS:
            raise ValueError("1..10 list rows allowed")
        wa_rows = [
            {"id": rid[:200], "title": _clip(title, ROW_TITLE), **({"description": _clip(desc, ROW_DESC)} if desc else {})}
            for rid, title, desc in rows
        ]
        payload = {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body[:BODY_MAX]},
                "action": {"button": _clip(button_label, LIST_BUTTON), "sections": [{"title": _clip(section_title or "Options", 24), "rows": wa_rows}]},
            },
        }
        return await self._send(r, payload, {"kind": "list", "text": body, "rows": [(w["id"], w["title"], w.get("description", "")) for w in wa_rows]})

    async def send_template(
        self, r: Recipient, name: str, params: list[str], button_payloads: list[str] | None = None
    ) -> str:
        self._guard(r, free_form=False)
        button_payloads = button_payloads or []
        payload = tpl.meta_template_payload(r.phone, name, r.lang, params, button_payloads)
        payload.pop("messaging_product")
        payload.pop("to")
        meta = {
            "kind": "template",
            "template": name,
            "text": tpl.render(name, r.lang, params),
            "buttons": list(zip(button_payloads, tpl.button_labels(name, r.lang))),
        }
        return await self._send(r, payload, meta)

    async def mark_as_read(self, wa_message_id: str) -> None:
        try:
            await self.transport.mark_as_read(wa_message_id)
        except Exception as exc:  # never fail inbound processing because of a read receipt
            log.warning("mark_as_read failed: %s", type(exc).__name__)

    # --------------------------------------------------------- template-or-free
    async def notify(
        self, r: Recipient, name: str, params: list[str], button_payloads: list[str] | None = None, extra_text: str = ""
    ) -> str:
        """Send a template's content: free-form (with real buttons) inside the window, template outside."""
        button_payloads = button_payloads or []
        if r.in_window():
            body = tpl.render(name, r.lang, params)
            if extra_text:
                body += "\n\n" + extra_text
            if button_payloads:
                return await self.send_buttons(r, body, list(zip(button_payloads, tpl.button_labels(name, r.lang))))
            return await self.send_text(r, body)
        return await self.send_template(r, name, params, button_payloads)


_client: WhatsAppClient | None = None


def get_wa() -> WhatsAppClient:
    global _client
    if _client is None:
        if get_settings().WA_MODE == "live":
            _client = WhatsAppClient(LiveTransport())
        else:
            from app.dev.fake_whatsapp import FakeTransport

            _client = WhatsAppClient(FakeTransport())
    return _client


def set_wa(client: WhatsAppClient | None) -> None:
    global _client
    _client = client
