"""FastAPI application factory.

Run locally with ``python -m app`` (binds 127.0.0.1:5174) or
``uvicorn app.main:app --host 127.0.0.1 --port 5174``. The environment profile
(``GOVERNANCE_ENVIRONMENT``) is validated before anything starts; see ``app.core.config``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import files, frontend, health, portal, session, workspace
from app.core.config import Settings, get_settings
from app.core.container import Services
from app.core.errors import ApiError
from app.core.logging import configure_logging
from app.core.observability import GENERIC_ERROR, ObservabilityMiddleware
from app.core.security import LocalSecurityMiddleware
from app.services.intelligence_service import run_automation

log = logging.getLogger("governance")


async def _reminder_loop(services: Services) -> None:
    """In-app reminders, checked on an interval while the server runs (Node: setInterval 60 s)."""
    interval = services.settings.governance_reminder_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await run_in_threadpool(run_automation, services, services.settings.governance_workspace_id)
        except Exception as error:  # noqa: BLE001 - a failed check must not stop the loop
            log.error("Reminder check: %s", error)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    settings.validate_runtime()  # unsafe staging/production configuration stops here

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        services = Services.build(settings)
        app.state.services = services
        task = asyncio.create_task(_reminder_loop(services)) if settings.governance_reminders_enabled else None
        log.info("Governance Desk API started (%s)", settings.environment, extra={"event": "startup", "environment": settings.environment})
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            services.db.dispose()

    docs = settings.api_docs_enabled  # development/test by default; off in staging/production
    app = FastAPI(title="Governance Desk API", version="2.0.0", lifespan=lifespan, redoc_url=None,
                  docs_url="/api/docs" if docs else None, openapi_url="/api/openapi.json" if docs else None)
    # Added last = outermost: every response (including rejections and 500s) gets a request ID,
    # security headers and a request log line.
    app.add_middleware(LocalSecurityMiddleware, settings=settings)
    app.add_middleware(ObservabilityMiddleware, settings=settings)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse({"error": error.message}, status_code=error.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException):
        return JSONResponse({"error": str(error.detail)}, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, error: Exception):
        log.exception("Unhandled error", extra={"event": "unhandled_error"})
        return JSONResponse(GENERIC_ERROR, status_code=500)

    for router in (health.router, workspace.router, files.router, portal.router, session.router, frontend.router):
        app.include_router(router)
    return app


app = create_app()
