"""Request helpers shared by the routers."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.auth import require_principal
from app.core.container import Services
from app.core.errors import BadRequest
from app.permissions import Context
from app.services.context import load_context

NO_STORE = {"Cache-Control": "no-store"}


def services_of(request: Request) -> Services:
    return request.app.state.services


def _context(services: Services, request: Request) -> Context:
    return load_context(services, require_principal(services, request))


async def context_of(request: Request) -> Context:
    """Authenticates the caller (401 without a session) and loads the workspace context."""
    return await run_in_threadpool(_context, services_of(request), request)


async def json_body(request: Request) -> dict:
    raw = await request.body()
    try:
        body: Any = json.loads(raw or b"null")
    except ValueError:
        raise BadRequest("The request body must be valid JSON.") from None
    return body if isinstance(body, dict) else {}


def json_response(content: Any, no_store: bool = False) -> JSONResponse:
    return JSONResponse(content, headers=NO_STORE if no_store else None)


def attachment(content: bytes | str, media_type: str, filename: str) -> Response:
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })
