"""PII encryption (Fernet) and phone hashing / masking."""
from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().FERNET_KEY.encode())


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def normalize_phone(raw: str) -> str:
    """Meta sends numbers without '+'. Store and compare in E.164 with '+'."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return "+" + digits


def phone_hash(e164: str) -> str:
    pepper = get_settings().PHONE_HASH_PEPPER.encode()
    return hmac.new(pepper, normalize_phone(e164).encode(), hashlib.sha256).hexdigest()


def mask_phone(e164: str | None) -> str:
    if not e164:
        return "<none>"
    p = normalize_phone(e164)
    if len(p) <= 7:
        return "***"
    return p[:3] + "*" * (len(p) - 7) + p[-4:]
