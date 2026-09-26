"""Slot generation: pure functions, no I/O (section 10.1)."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from app.config import ClinicConfig
from app.timeutil import local_to_utc, to_local

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class SessionWindow:
    name: str
    session_date: date
    start_utc: datetime
    end_utc: datetime


def session_name_for(cfg: ClinicConfig, local_t: time) -> str:
    """Session a range/slot starting at local_t belongs to (first matching starts_before)."""
    names = list(cfg.queue.sessions)
    for name in names:
        sb = cfg.queue.sessions[name].starts_before
        if sb is not None and local_t < sb:
            return name
    for name in names:
        if cfg.queue.sessions[name].starts_before is None:
            return name
    return names[-1]


def session_windows(cfg: ClinicConfig, d: date, *, include_holidays: bool = False) -> list[SessionWindow]:
    if d in cfg.holidays and not include_holidays:
        return []
    ranges = cfg.hours.get(WEEKDAYS[d.weekday()], [])
    by_session: dict[str, list[tuple[time, time]]] = {}
    for start, end in ranges:
        by_session.setdefault(session_name_for(cfg, start), []).append((start, end))
    out = []
    for name, rs in by_session.items():
        out.append(
            SessionWindow(
                name=name,
                session_date=d,
                start_utc=local_to_utc(d, min(r[0] for r in rs)),
                end_utc=local_to_utc(d, max(r[1] for r in rs)),
            )
        )
    out.sort(key=lambda w: w.start_utc)
    return out


def session_window(cfg: ClinicConfig, d: date, session: str) -> SessionWindow | None:
    for w in session_windows(cfg, d, include_holidays=True):
        if w.name == session:
            return w
    return None


def day_slot_starts(cfg: ClinicConfig, d: date, *, include_holidays: bool = False) -> list[datetime]:
    """All slot starts (UTC) for local date d, in order, ignoring lead time / busy."""
    if d in cfg.holidays and not include_holidays:
        return []
    step = timedelta(minutes=cfg.slot_minutes + cfg.buffer_minutes)
    length = timedelta(minutes=cfg.slot_minutes)
    out: list[datetime] = []
    for start, end in cfg.hours.get(WEEKDAYS[d.weekday()], []):
        cur = local_to_utc(d, start)
        stop = local_to_utc(d, end)
        while cur + length <= stop:
            out.append(cur)
            cur += step
    return out


def session_of_slot(cfg: ClinicConfig, start_utc: datetime) -> tuple[date, str]:
    local = to_local(start_utc)
    return local.date(), session_name_for(cfg, _range_start_for(cfg, local))


def _range_start_for(cfg: ClinicConfig, local: datetime) -> time:
    """Sessions are assigned per opening range, so use the start of the range containing this time."""
    for start, end in cfg.hours.get(WEEKDAYS[local.weekday()], []):
        if start <= local.time() < end:
            return start
    return local.time()


def session_grid(cfg: ClinicConfig, d: date, session: str) -> list[datetime]:
    return [s for s in day_slot_starts(cfg, d, include_holidays=True) if session_of_slot(cfg, s)[1] == session]


def is_valid_slot_start(cfg: ClinicConfig, start_utc: datetime) -> bool:
    return start_utc in day_slot_starts(cfg, to_local(start_utc).date())


def candidate_slots(cfg: ClinicConfig, date_from: date, date_to: date, now_utc: datetime) -> list[datetime]:
    """Steps 1-2: hours, holidays, past, lead time, horizon."""
    today = to_local(now_utc).date()
    last_day = min(date_to, today + timedelta(days=cfg.booking_horizon_days))
    first_day = max(date_from, today)
    earliest = now_utc + timedelta(minutes=cfg.min_lead_minutes)
    out: list[datetime] = []
    d = first_day
    while d <= last_day:
        out.extend(s for s in day_slot_starts(cfg, d) if s >= earliest)
        d += timedelta(days=1)
    return out


def remove_busy(
    cfg: ClinicConfig, slots: Iterable[datetime], busy: Iterable[tuple[datetime, datetime]]
) -> list[datetime]:
    """Step 3: drop slots overlapping busy blocks (buffer applied on both sides)."""
    buf = timedelta(minutes=cfg.buffer_minutes)
    length = timedelta(minutes=cfg.slot_minutes)
    blocks = [(b0 - buf, b1 + buf) for b0, b1 in busy]
    return [s for s in slots if not any(s < b1 and s + length > b0 for b0, b1 in blocks)]


def available_slots(
    cfg: ClinicConfig,
    date_from: date,
    date_to: date,
    now_utc: datetime,
    busy: Iterable[tuple[datetime, datetime]],
    booked_starts: Iterable[datetime],
    part_of_day: str | None = None,
    not_before: time | None = None,
) -> list[datetime]:
    booked = set(booked_starts)
    slots = remove_busy(cfg, candidate_slots(cfg, date_from, date_to, now_utc), busy)
    slots = [s for s in slots if s not in booked]  # step 4
    if part_of_day and part_of_day != "any":
        slots = [s for s in slots if session_of_slot(cfg, s)[1] == part_of_day]
    if not_before:
        slots = [s for s in slots if to_local(s).time() >= not_before]
    return slots


# ------------------------------------------------------------------ signed slot ids

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = _B36[r] + s
    return s or "0"


def sign_slot_id(start_utc: datetime, secret: str) -> str:
    body = _b36(int(start_utc.timestamp()) // 60)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:10]
    return f"{body}.{sig}"


def verify_slot_id(slot_id: str, secret: str) -> datetime | None:
    from datetime import timezone

    try:
        body, sig = slot_id.strip().split(".")
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:10]
        if not hmac.compare_digest(sig, expected):
            return None
        return datetime.fromtimestamp(int(body, 36) * 60, tz=timezone.utc)
    except (ValueError, AttributeError):
        return None
