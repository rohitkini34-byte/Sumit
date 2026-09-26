"""Seed fake patients + appointments for dev/staging (fake names only; never production data).

    python -m scripts.seed_dev_data            # uses DATABASE_URL from .env
"""
import asyncio
import random
from datetime import timedelta

from app.calendar.slots import day_slot_starts
from app.config import get_clinic, get_settings
from app.db.crypto import encrypt
from app.db.models import Base
from app.db.session import engine, get_session
from app.dev import clock
from app.scheduling import booking_service
from app.scheduling.common import DomainError, get_or_create_patient
from app.timeutil import local_today

NAMES = ["Asha Patil", "Ravi Kumar", "Sunita Deshmukh", "Imran Shaikh", "Meera Joshi", "Vikram Singh",
         "Pooja Kulkarni", "Arjun Nair", "Kavita Pawar", "Rahul Verma", "Neha Gupta", "Sanjay More"]


async def main() -> None:
    if get_settings().APP_ENV == "prod":
        raise SystemExit("refusing to seed a production database")
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    cfg = get_clinic()
    rnd = random.Random(42)
    today = local_today(clock.now())
    made = 0
    for i, name in enumerate(NAMES):
        phone = f"+9198000{i:05d}"
        async with get_session() as db:
            p, _ = await get_or_create_patient(db, phone)
            p.name_enc = encrypt(name)
            p.consent_given_at = clock.now()
            p.consent_version = "seed"
            p.preferred_language = rnd.choice(["en", "hi", "mr"])
            await db.commit()
            pid = p.id
        for day_offset in (0, 1, 2):
            starts = [s for s in day_slot_starts(cfg, today + timedelta(days=day_offset)) if s > clock.now()]
            if not starts:
                continue
            try:
                await booking_service.book(pid, rnd.choice(starts), source="admin", actor="seed", enforce_limit=False)
                made += 1
                break
            except DomainError:
                continue
    print(f"seeded {len(NAMES)} patients, {made} appointments (phones +9198000000xx)")


if __name__ == "__main__":
    asyncio.run(main())
