"""X-Hub-Signature-256 verification (section 7.1)."""
from __future__ import annotations

import hashlib
import hmac


def compute_signature(raw_body: bytes, app_secret: str) -> str:
    return "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    if not header_value or not app_secret:
        return False
    return hmac.compare_digest(compute_signature(raw_body, app_secret), header_value.strip())
