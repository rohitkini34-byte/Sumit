"""Async engine / session factory and advisory locking."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
# Separate small pool for the dev fake calendar, so calendar writes made while a booking
# holds a pooled connection + advisory lock can never be starved by other waiters.
_aux_engine: AsyncEngine | None = None
_aux_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_url: str | None = None


def _make_engine(url: str, pool_size: int, overflow: int) -> AsyncEngine:
    if url.startswith("sqlite"):
        eng = create_async_engine(url, connect_args={"timeout": 30})

        @event.listens_for(eng.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng
    return create_async_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=overflow)


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _sessionmaker, _url
    _url = url or get_settings().DATABASE_URL
    _engine = _make_engine(_url, 10, 20)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def aux_session() -> AsyncIterator[AsyncSession]:
    global _aux_engine, _aux_sessionmaker
    if _aux_sessionmaker is None:
        engine()
        _aux_engine = _make_engine(_url, 2, 3)
        _aux_sessionmaker = async_sessionmaker(_aux_engine, expire_on_commit=False)
    async with _aux_sessionmaker() as s:
        yield s


def engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as s:
        yield s


async def dispose() -> None:
    global _engine, _sessionmaker, _aux_engine, _aux_sessionmaker
    for eng in (_engine, _aux_engine):
        if eng is not None:
            await eng.dispose()
    _engine = _aux_engine = None
    _sessionmaker = _aux_sessionmaker = None


# --------------------------------------------------------------------------- locks

_local_locks: dict[int, asyncio.Lock] = {}
_held: contextvars.ContextVar[frozenset[int]] = contextvars.ContextVar("held_locks", default=frozenset())


def lock_key(*parts) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock."""
    digest = hashlib.blake2b("|".join(str(p) for p in parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def slot_key(start_utc) -> int:
    return lock_key("slot", start_utc.isoformat())


def session_key(session_date, session: str) -> int:
    return lock_key("session", session_date.isoformat(), session)


@asynccontextmanager
async def hold(db: AsyncSession, *keys: int) -> AsyncIterator[None]:
    """Hold advisory locks for the rest of the current transaction.

    Postgres: pg_advisory_xact_lock, released automatically on commit/rollback.
    SQLite (zero-setup dev only): in-process asyncio locks held until the block exits,
    so callers must commit inside the block.
    """
    already = _held.get()
    ordered = sorted(set(keys) - already)
    token = _held.set(already | set(ordered))
    try:
        async with _hold(db, ordered):
            yield
    finally:
        _held.reset(token)


@asynccontextmanager
async def _hold(db: AsyncSession, ordered: list[int]) -> AsyncIterator[None]:
    if db.bind.dialect.name == "postgresql":
        for k in ordered:
            await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})
        yield
        return
    acquired: list[asyncio.Lock] = []
    try:
        for k in ordered:
            lk = _local_locks.setdefault(k, asyncio.Lock())
            await lk.acquire()
            acquired.append(lk)
        yield
    finally:
        for lk in reversed(acquired):
            lk.release()
