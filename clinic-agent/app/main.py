"""FastAPI app: routers, scheduler, health."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_clinic, get_settings
from app.db import session as dbsession
from app.db.models import Base
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def create_tables() -> None:
    async with dbsession.engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def create_app(*, start_scheduler: bool | None = None) -> FastAPI:
    settings = get_settings()
    get_clinic()  # fail fast on a broken clinic_config.yaml

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        dbsession.engine()
        if settings.APP_ENV == "dev":
            await create_tables()  # staging/prod use `alembic upgrade head`
        run_sched = settings.ENABLE_SCHEDULER if start_scheduler is None else start_scheduler
        if run_sched:
            from app import scheduler

            scheduler.start()
        log.info("started env=%s wa=%s calendar=%s llm=%s", settings.APP_ENV, settings.WA_MODE,
                 settings.CALENDAR_MODE, settings.LLM_MODE)
        yield
        if run_sched:
            from app import scheduler

            scheduler.stop()
        await dbsession.dispose()

    app = FastAPI(title="Clinic WhatsApp Agent", lifespan=lifespan, docs_url="/docs" if settings.APP_ENV == "dev" else None,
                  redoc_url=None, openapi_url="/openapi.json" if settings.APP_ENV == "dev" else None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SESSION_SECRET,
        same_site="strict",
        https_only=settings.APP_ENV != "dev",
        session_cookie="clinic_admin",
        max_age=12 * 3600,
    )

    from fastapi.responses import RedirectResponse

    from app.admin import queue_routes, routes as admin_routes
    from app.admin.auth import LoginRequired

    @app.exception_handler(LoginRequired)
    async def _login_required(request, exc):
        return RedirectResponse("/admin/login", status_code=303)

    from app.whatsapp import webhook

    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(webhook.router)
    app.include_router(admin_routes.router)
    app.include_router(queue_routes.router)
    if settings.APP_ENV == "dev":
        from app.dev import simulator

        app.include_router(simulator.router)

    @app.get("/health")
    async def health():
        from sqlalchemy import text

        async with dbsession.get_session() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "env": settings.APP_ENV}

    @app.get("/")
    async def root():
        links = {"admin": "/admin", "health": "/health"}
        if settings.APP_ENV == "dev":
            links.update({"simulator": "/dev/chat", "dev_tools": "/dev"})
        return links

    return app


app = create_app()
