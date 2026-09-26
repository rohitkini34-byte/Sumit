"""Single source of "now" for the whole app.

Never call datetime.now() anywhere else. In dev (and tests) the clock can be frozen or
shifted so reminder and queue jobs can be exercised without waiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_frozen: datetime | None = None
_offset = timedelta(0)


def now() -> datetime:
    """Current time, timezone-aware UTC."""
    base = _frozen if _frozen is not None else datetime.now(timezone.utc)
    return base + _offset


def set_now(dt: datetime) -> None:
    """Freeze the clock at dt (aware datetime, any tz)."""
    global _frozen, _offset
    if dt.tzinfo is None:
        raise ValueError("set_now needs an aware datetime")
    _frozen = dt.astimezone(timezone.utc)
    _offset = timedelta(0)


def advance(minutes: float = 0, **kw) -> datetime:
    """Move the clock forward (works frozen or running)."""
    global _frozen, _offset
    delta = timedelta(minutes=minutes, **kw)
    if _frozen is not None:
        _frozen += delta
    else:
        _offset += delta
    return now()


def reset() -> None:
    global _frozen, _offset
    _frozen = None
    _offset = timedelta(0)


def is_overridden() -> bool:
    return _frozen is not None or _offset != timedelta(0)


def snapshot() -> tuple:
    return (_frozen, _offset)


def restore(snap: tuple) -> None:
    global _frozen, _offset
    _frozen, _offset = snap
