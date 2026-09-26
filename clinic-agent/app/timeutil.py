"""Timezone helpers. The DB stores UTC; everything patient-facing is Asia/Kolkata."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

from app.config import get_settings


def tz():
    return get_settings().tz


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(tz())


def local_to_utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=tz()).astimezone(timezone.utc)


def local_today(now_utc: datetime) -> date:
    return to_local(now_utc).date()


def fmt_time(dt: datetime) -> str:
    return to_local(dt).strftime("%I:%M %p").lstrip("0")


def fmt_date(dt_or_d) -> str:
    d = to_local(dt_or_d) if isinstance(dt_or_d, datetime) else dt_or_d
    return d.strftime("%a %d %b %Y")


def fmt_short(dt: datetime) -> str:
    """Compact label used in list rows (<= 24 chars): 'Mon 28 Sep 10:30 AM'."""
    return to_local(dt).strftime("%a %d %b ") + fmt_time(dt)


def ceil_to(dt: datetime, minutes: int) -> datetime:
    if minutes <= 1:
        return dt.replace(second=0, microsecond=0) + (timedelta(minutes=1) if dt.second or dt.microsecond else timedelta(0))
    epoch = dt.timestamp()
    step = minutes * 60
    return datetime.fromtimestamp(math.ceil(epoch / step) * step, tz=timezone.utc)


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0
