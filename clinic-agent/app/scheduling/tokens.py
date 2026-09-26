"""Token number assignment: pure functions (section 10A.1 / 10B.3)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.calendar.slots import session_grid, session_of_slot
from app.config import ClinicConfig
from app.timeutil import local_to_utc


def label(prefix: str, n: int) -> str:
    return f"{prefix}-{n:02d}"


def session_prefix(cfg: ClinicConfig, session: str) -> str:
    return cfg.queue.sessions[session].prefix


def slot_token(cfg: ClinicConfig, start_utc: datetime) -> tuple[date, str, int, str]:
    """Token = 1-based position of the slot in its session's slot grid."""
    d, session = session_of_slot(cfg, start_utc)
    grid = session_grid(cfg, d, session)
    n = grid.index(start_utc) + 1
    return d, session, n, label(session_prefix(cfg, session), n)


def sequential_tokens(slot_starts: list[datetime]) -> dict[datetime, int]:
    """Tokens 1..N in slot order with no gaps."""
    return {s: i + 1 for i, s in enumerate(sorted(slot_starts))}


def tokens_frozen(cfg: ClinicConfig, session_date: date, now_utc: datetime) -> bool:
    """Sequential mode: frozen from token_freeze_time on the day before the session."""
    freeze_at = local_to_utc(session_date - timedelta(days=1), cfg.changes.token_freeze_time)
    return now_utc >= freeze_at


def walk_in_label(cfg: ClinicConfig, n: int) -> str:
    return label(cfg.queue.walk_in_prefix, n)
