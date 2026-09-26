"""APScheduler jobs. Idempotency lives in the DB (reminder_log / outbox unique keys),
so running more than one instance never double-sends."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.scheduling import offers, outbox, reminders

log = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def _safe(fn, *a):
    try:
        await fn(*a)
    except Exception:
        log.exception("job %s failed", getattr(fn, "__name__", fn))


def start() -> AsyncIOScheduler:
    global _scheduler
    tz = get_settings().tz
    s = AsyncIOScheduler(timezone=tz)
    s.add_job(_safe, "interval", minutes=5, args=[reminders.run_every_5_minutes], id="every5", max_instances=1, coalesce=True)
    s.add_job(_safe, "interval", minutes=1, args=[outbox.process_outbox], id="outbox", max_instances=1, coalesce=True)
    s.add_job(_safe, "interval", minutes=30, args=[reminders.reconcile_calendar], id="reconcile", max_instances=1)
    s.add_job(_safe, CronTrigger(hour=9, minute=30, timezone=tz), args=[reminders.run_followups], id="followups")
    s.add_job(_safe, "interval", hours=1, args=[reminders.purge_contexts], id="purge")
    s.add_job(_safe, CronTrigger(day=1, hour=3, minute=0, timezone=tz), args=[reminders.run_retention], id="retention")
    s.start()
    _scheduler = s

    def arm(expires_at: datetime) -> None:
        run_at = max(expires_at, datetime.now(timezone.utc)) + timedelta(seconds=5)
        s.add_job(_safe, "date", run_date=run_at, args=[offers.expire_offers])

    offers.expiry_timer_hook = arm
    return s


def stop() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    offers.expiry_timer_hook = None
