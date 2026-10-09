"""Liveness and readiness probes. They reveal no paths, versions or error details."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import NO_STORE, services_of
from app.core.container import Services
from app.database.migrations import current_revision, head_revision

router = APIRouter()
log = logging.getLogger("governance.health")


@router.get("/health", include_in_schema=False)
async def health():
    """Liveness: the process is up and serving requests (no dependencies checked)."""
    return JSONResponse({"status": "ok"}, headers=NO_STORE)


def _checks(services: Services) -> dict[str, str]:
    checks: dict[str, str] = {}
    try:
        services.db.ping()
        checks["database"] = "ok" if current_revision(services.db.engine) == head_revision() else "migration_pending"
    except Exception as error:  # noqa: BLE001 - reported as a failed check, details only in the log
        log.warning("Readiness: database check failed (%s)", type(error).__name__, extra={"check": "database"})
        checks["database"] = "unavailable"
    files = services.settings.files_dir
    if files.is_dir() and os.access(files, os.R_OK | os.W_OK | os.X_OK):
        checks["storage"] = "ok"
    else:
        log.warning("Readiness: document storage is missing or not writable", extra={"check": "storage"})
        checks["storage"] = "unavailable"
    return checks


@router.get("/ready", include_in_schema=False)
async def ready(request: Request):
    """Readiness: the database answers at the expected schema revision and storage is writable."""
    checks = await run_in_threadpool(_checks, services_of(request))
    ok = all(value == "ok" for value in checks.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503, headers=NO_STORE)
