import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.whatsapp.parser import parse_webhook
from app.whatsapp.signature import compute_signature, verify_signature

SECRET = "test-app-secret"


def test_signature_valid_tampered_missing():
    body = b'{"a":1}'
    sig = compute_signature(body, SECRET)
    assert verify_signature(body, sig, SECRET)
    assert not verify_signature(b'{"a":2}', sig, SECRET)
    assert not verify_signature(body, None, SECRET)
    assert not verify_signature(body, "sha256=deadbeef", SECRET)


@pytest.fixture
async def client():
    from app.main import create_app

    app = create_app(start_scheduler=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_verify_endpoint(client):
    ok = await client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "123"})
    assert ok.status_code == 200 and ok.text == "123"
    bad = await client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


async def test_post_rejects_unsigned_and_tampered(client):
    from app.dev.simulator import build_payload

    body = json.dumps(build_payload("+919800000001", "A", text="hi")).encode()
    assert (await client.post("/webhook/whatsapp", content=body)).status_code == 401
    sig = compute_signature(body, SECRET)
    assert (await client.post("/webhook/whatsapp", content=body + b" ", headers={"X-Hub-Signature-256": sig})).status_code == 401


async def test_post_signed_message_is_processed(client, wa):
    from app.dev.simulator import build_payload

    body = json.dumps(build_payload("+919800000001", "Asha", text="Hi")).encode()
    r = await client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": compute_signature(body, SECRET)})
    assert r.status_code == 200
    # background task ran: consent request went out
    msgs = wa.for_phone("+919800000001")
    assert msgs and [b[0] for b in msgs[-1].buttons] == ["consent_yes", "consent_no"]


async def test_retry_of_same_message_is_ignored(client, wa):
    from app.dev.simulator import build_payload

    payload = build_payload("+919800000001", "Asha", text="Hi")
    body = json.dumps(payload).encode()
    h = {"X-Hub-Signature-256": compute_signature(body, SECRET)}
    await client.post("/webhook/whatsapp", content=body, headers=h)
    await client.post("/webhook/whatsapp", content=body, headers=h)
    assert len(wa.for_phone("+919800000001")) == 1


def test_parser_types():
    from app.dev.simulator import build_payload

    m, _ = parse_webhook(build_payload("919800000001", "A", text="hello"))
    assert m[0].from_phone == "+919800000001" and m[0].text == "hello" and m[0].supported
    m, _ = parse_webhook(build_payload("+919800000001", "A", reply_id="slot:x", reply_title="t", kind="list_reply"))
    assert m[0].reply_id == "slot:x" and m[0].is_structured
    m, _ = parse_webhook(build_payload("+919800000001", "A", reply_id="consent_yes", kind="button_reply"))
    assert m[0].reply_id == "consent_yes"
    m, _ = parse_webhook(build_payload("+919800000001", "A", reply_id="r24_confirm:1", reply_title="Confirm", kind="template_button"))
    assert m[0].type == "button" and m[0].reply_id == "r24_confirm:1"
    m, _ = parse_webhook(build_payload("+919800000001", "A", kind="image"))
    assert not m[0].supported
    _, st = parse_webhook({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "read"}]}}]}]})
    assert st[0].status == "read"


async def test_unsupported_type_reply(wa):
    from app.agent.router import handle_inbound
    from tests.conftest import _msg, make_patient

    await make_patient()
    await handle_inbound(_msg("+919800000001", type="image"))
    assert "only read text" in wa.for_phone("+919800000001")[-1].text


async def test_status_updates_reminder_log(wa):
    from app.db.models import ReminderLog
    from app.db.session import get_session
    from app.whatsapp.parser import StatusUpdate
    from app.whatsapp.webhook import process_payload

    async with get_session() as db:
        db.add(ReminderLog(kind="r24h", wa_message_id="wamid.x", status="sent"))
        await db.commit()
    await process_payload([], [StatusUpdate("wamid.x", "delivered")])
    from tests.conftest import rows

    assert (await rows(ReminderLog))[0].status == "delivered"
