"""Post a signed sample WhatsApp webhook to a running server.

    python -m scripts.send_test_webhook "Hi"                       # text from +919800000001
    python -m scripts.send_test_webhook --reply consent_yes        # button reply
    python -m scripts.send_test_webhook --url https://x.trycloudflare.com "Kal subah appointment chahiye"
"""
import argparse
import json

import httpx

from app.config import get_settings
from app.dev.simulator import build_payload
from app.whatsapp.signature import compute_signature


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="Hi")
    ap.add_argument("--phone", default="+919800000001")
    ap.add_argument("--name", default="Test Patient")
    ap.add_argument("--reply", help="interactive reply id (button_reply)")
    ap.add_argument("--url", default="http://localhost:8000")
    a = ap.parse_args()
    if a.reply:
        payload = build_payload(a.phone, a.name, reply_id=a.reply, reply_title=a.reply, kind="button_reply")
    else:
        payload = build_payload(a.phone, a.name, text=a.text)
    body = json.dumps(payload).encode()
    sig = compute_signature(body, get_settings().WA_APP_SECRET)
    r = httpx.post(f"{a.url.rstrip('/')}/webhook/whatsapp", content=body,
                   headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
