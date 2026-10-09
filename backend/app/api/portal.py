"""``/api/portal``: client portal state, actions and downloads."""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import attachment, context_of, json_body, json_response, services_of
from app.services.dispatch import portal_mutate
from app.services.portal.exports import portal_export
from app.services.portal.state import portal_state

router = APIRouter()


@router.get("/api/portal")
async def state(request: Request):
    ctx = await context_of(request)
    params = dict(request.query_params)
    if "export" in params:
        download = await run_in_threadpool(portal_export, services_of(request), ctx, params)
        return attachment(download.content, download.media_type, download.filename)
    return json_response(await run_in_threadpool(portal_state, ctx), no_store=True)


@router.post("/api/portal")
async def action(request: Request):
    ctx = await context_of(request)
    body = await json_body(request)
    return json_response(await run_in_threadpool(portal_mutate, services_of(request), ctx, body))
