"""FastAPI application factory.

Run locally with ``python -m app`` (binds 127.0.0.1:5174) or
``uvicorn app.main:app --host 127.0.0.1 --port 5174``. The environment profile
(``GOVERNANCE_ENVIRONMENT``) is validated before anything starts; see ``app.core.config``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import auth, files, frontend, health, portal, session, workspace
from app.core.config import Settings, get_settings
from app.core.container import Services
from app.core.csrf import CsrfMiddleware
from app.core.errors import ApiError
from app.core.logging import configure_logging
from app.core.observability import GENERIC_ERROR, ObservabilityMiddleware
from app.core.security import LocalSecurityMiddleware
from app.services.identity_service import ensure_preview_organization
from app.services.intelligence_service import run_scheduled_reminders

log = logging.getLogger("governance")


async def _reminder_loop(services: Services) -> None:
    """In-app reminders for every active organization, checked on an interval (Node: setInterval 60 s).

    Several processes may run this loop; a database lease lets only one of them do each pass.
    """
    interval = services.settings.governance_reminder_interval_seconds
    holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    while True:
        await asyncio.sleep(interval)
        try:
            await run_in_threadpool(run_scheduled_reminders, services, holder, interval * 3)
        except Exception:  # noqa: BLE001 - a failed check must not stop the loop
            log.exception("Reminder check failed", extra={"event": "reminder_error"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    settings.validate_runtime()  # unsafe staging/production configuration stops here

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        services = Services.build(settings)
        ensure_preview_organization(services)  # development/test only
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
    # security headers and a request log line. Host/origin checks run before the CSRF check.
    app.add_middleware(CsrfMiddleware, settings=settings)
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

    for router in (health.router, auth.router, workspace.router, files.router, portal.router, session.router, frontend.router):
        app.include_router(router)
    return app


app = create_app()
