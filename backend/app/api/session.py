"""``/api/session``: local-preview profile switching ("Preview as")."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.deps import json_body, json_response, services_of
from app.auth import set_session_cookie, switch_profile

router = APIRouter()


class SessionRequest(BaseModel):
    actor: Any = None


@router.post("/api/session")
async def switch(request: Request):
    payload = SessionRequest.model_validate(await json_body(request))
    token = await run_in_threadpool(switch_profile, services_of(request), request, payload.actor)
    response = json_response({"ok": True})
    set_session_cookie(response, token, services_of(request).settings.secure_cookies)
    return response
