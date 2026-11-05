"""``/api/portal/team``: Team Members of a client company (see ``app.services.team_service``)."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import context_of, json_body, json_response, services_of
from app.auth import client_address
from app.core.errors import NotFound
from app.services import team_service

router = APIRouter()

ACTIONS: dict[str, Callable] = {
    "create": team_service.team_create,
    "update": team_service.team_update,
    "password": team_service.team_password,
    "status": team_service.team_status,
    "remove": team_service.team_remove,
}


@router.get("/api/portal/team")
async def team(request: Request):
    ctx = await context_of(request)
    return json_response(await run_in_threadpool(team_service.team_list, services_of(request), ctx, request.query_params.get("company")), no_store=True)


@router.post("/api/portal/team/{action}")
async def team_action(action: str, request: Request):
    handler = ACTIONS.get(action)
    if handler is None:
        raise NotFound("Not found.")
    ctx = await context_of(request)
    body = await json_body(request)
    services = services_of(request)
    return json_response(await run_in_threadpool(handler, services, ctx, body, client_address(services.settings, request)), no_store=True)
