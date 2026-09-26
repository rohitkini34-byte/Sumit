"""Client guards + contract tests: the fake transport and the live (respx-mocked) transport
go through the same WhatsAppClient and must behave identically."""
from datetime import timedelta

import httpx
import pytest
import respx

from app.config import get_settings
from app.dev import clock
from app.dev.fake_whatsapp import FakeTransport
from app.whatsapp.client import LiveTransport, OutsideWindow, Recipient, SendBlocked, SendFailed, WhatsAppClient

GRAPH = "https://graph.facebook.com/v21.0/PNID/messages"


@pytest.fixture
def live_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "WA_MODE", "live")
    monkeypatch.setattr(s, "WA_PHONE_NUMBER_ID", "PNID")
    monkeypatch.setattr(s, "WA_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(s, "DEV_ALLOWED_NUMBERS", "+919800000001")
    monkeypatch.setattr("app.whatsapp.client.asyncio.sleep", _nosleep)
    return s


async def _nosleep(_):
    return None


def rcpt(**kw):
    kw.setdefault("last_inbound_at", clock.now())
    return Recipient(phone=kw.pop("phone", "+919800000001"), **kw)


@pytest.fixture(params=["fake", "live"])
def client(request, live_settings):
    if request.param == "fake":
        return WhatsAppClient(FakeTransport())
    router = respx.mock(assert_all_called=False)
    router.start()
    router.post(GRAPH).mock(return_value=httpx.Response(200, json={"messages": [{"id": "wamid.live.1"}]}))
    request.addfinalizer(router.stop)
    return WhatsAppClient(LiveTransport())


async def test_contract_send_text_buttons_list_template(client):
    assert await client.send_text(rcpt(), "hello")
    assert await client.send_buttons(rcpt(), "pick", [("a", "A"), ("b", "B")])
    assert await client.send_list(rcpt(), "pick", "Times", [(f"r{i}", f"Row {i}", "") for i in range(10)], "S")
    assert await client.send_template(rcpt(last_inbound_at=None), "appt_cancelled", ["A", "Mon", "M-01"], ["book_again:1"])


async def test_contract_limits(client):
    with pytest.raises(ValueError):
        await client.send_buttons(rcpt(), "x", [("a", "A")] * 4)
    with pytest.raises(ValueError):
        await client.send_list(rcpt(), "x", "b", [(f"r{i}", "t", "") for i in range(11)], "S")


async def test_contract_window_and_optout(client):
    old = rcpt(last_inbound_at=clock.now() - timedelta(hours=25))
    with pytest.raises(OutsideWindow):
        await client.send_text(old, "hi")
    assert await client.send_template(old, "appt_no_show", ["A", "Mon"], ["book_again:1"])
    with pytest.raises(SendBlocked):
        await client.send_text(rcpt(opted_out=True), "hi")
    assert await client.send_text(rcpt(opted_out=True), "ack", allow_opted_out=True)


async def test_notify_picks_free_form_inside_window_template_outside():
    t = FakeTransport()
    c = WhatsAppClient(t)
    await c.notify(rcpt(), "appt_cancelled", ["A", "Mon", "M-01"], ["book_again:1"])
    await c.notify(rcpt(last_inbound_at=None), "appt_cancelled", ["A", "Mon", "M-01"], ["book_again:1"])
    assert [m.kind for m in t.sent] == ["buttons", "template"]
    assert t.sent[0].buttons[0][0] == "book_again:1" and t.sent[1].buttons[0][0] == "book_again:1"


async def test_titles_clipped():
    t = FakeTransport()
    c = WhatsAppClient(t)
    await c.send_list(rcpt(), "b", "A very long button label here", [("x", "x" * 40, "d" * 100)], "S")
    title, desc = t.sent[0].rows[0][1], t.sent[0].rows[0][2]
    assert len(title) <= 24 and len(desc) <= 72
    lst = t.sent[0].payload["interactive"]["action"]
    assert len(lst["button"]) <= 20


@respx.mock
async def test_live_allowlist_enforced(live_settings):
    route = respx.post(GRAPH).mock(return_value=httpx.Response(200, json={"messages": [{"id": "w"}]}))
    c = WhatsAppClient(LiveTransport())
    with pytest.raises(SendBlocked):
        await c.send_text(rcpt(phone="+919811111111"), "hi")
    assert not route.called


@respx.mock
async def test_live_retries_5xx_and_429(live_settings):
    route = respx.post(GRAPH).mock(side_effect=[httpx.Response(500), httpx.Response(429),
                                                httpx.Response(200, json={"messages": [{"id": "ok"}]})])
    c = WhatsAppClient(LiveTransport())
    assert await c.send_text(rcpt(), "hi") == "ok"
    assert route.call_count == 3


@respx.mock
async def test_live_gives_up_after_three(live_settings):
    respx.post(GRAPH).mock(return_value=httpx.Response(503))
    with pytest.raises(SendFailed):
        await WhatsAppClient(LiveTransport()).send_text(rcpt(), "hi")


@respx.mock
async def test_live_4xx_not_retried(live_settings):
    route = respx.post(GRAPH).mock(return_value=httpx.Response(400, json={"error": {"code": 100}}))
    with pytest.raises(SendFailed):
        await WhatsAppClient(LiveTransport()).send_text(rcpt(), "hi")
    assert route.call_count == 1


@respx.mock
async def test_staging_template_falls_back_to_hello_world(live_settings, monkeypatch):
    monkeypatch.setattr(live_settings, "APP_ENV", "staging")
    route = respx.post(GRAPH).mock(side_effect=[
        httpx.Response(404, json={"error": {"code": 132001}}),
        httpx.Response(200, json={"messages": [{"id": "hw"}]}),
    ])
    out = await WhatsAppClient(LiveTransport()).send_template(rcpt(), "appt_no_show", ["A", "Mon"], ["book_again:1"])
    assert out == "hw"
    import json

    assert json.loads(route.calls[1].request.content)["template"]["name"] == "hello_world"


@respx.mock
async def test_live_payload_shapes(live_settings):
    route = respx.post(GRAPH).mock(return_value=httpx.Response(200, json={"messages": [{"id": "w"}]}))
    c = WhatsAppClient(LiveTransport())
    await c.send_template(rcpt(), "appt_reminder_24h", ["A", "Mon", "M-01", "10:00"], ["r24_confirm:1", "r24_resched:1", "r24_cancel:1"])
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["to"] == "919800000001" and body["type"] == "template"
    comps = body["template"]["components"]
    assert comps[0]["type"] == "body" and len(comps[0]["parameters"]) == 4
    assert [x["parameters"][0]["payload"] for x in comps[1:]] == ["r24_confirm:1", "r24_resched:1", "r24_cancel:1"]
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@pytest.mark.staging
async def test_staging_real_send():
    """Runs only with real Meta test credentials (pytest -m staging)."""
    s = get_settings()
    if not (s.WA_ACCESS_TOKEN and s.WA_PHONE_NUMBER_ID and s.allowed_numbers):
        pytest.skip("no staging credentials")
    c = WhatsAppClient(LiveTransport())
    assert await c.send_template(Recipient(phone=sorted(s.allowed_numbers)[0]), "appt_no_show", ["Test", "Mon"], ["book_again:x"])
