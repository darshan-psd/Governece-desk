"""Authentication boundary.

Everything about *who is calling* lives here, so production authentication can replace the
local-preview provider without changing services or routes.

Local-preview mode (``GOVERNANCE_AUTH_MODE=local-preview``, the only mode in this phase):
  * Opening the app page (``GET /``) on the loopback address issues a session for the
    workspace owner when the browser has no valid session. This is the local "sign in".
  * ``POST /api/session`` switches the signed-in browser to another active profile
    ("Preview as"). It requires an existing valid session.
  * API requests without a valid session are rejected with HTTP 401. Unlike the Node
    prototype, a missing or unknown cookie never falls back to the Owner.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Request, Response

from app.core.container import Services
from app.core.errors import Forbidden, NotFound, Unauthorized
from app.repositories.records import RecordRepository
from app.repositories.sessions import SessionRepository

COOKIE_NAME = "gov_preview"
SIGN_IN_MESSAGE = "Please sign in to access your workspace."
BUILTIN_OWNER = {"id": "owner", "name": "Workspace owner", "role": "Owner", "companies": ["*"], "active": True}


@dataclass(frozen=True)
class Principal:
    workspace: str
    actor_id: str


def current_principal(services: Services, request: Request) -> Principal | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    with services.db.read() as session:
        row = SessionRepository(session).find(token)
        return Principal(row.workspace, row.actor) if row else None


def require_principal(services: Services, request: Request) -> Principal:
    principal = current_principal(services, request)
    if principal is None:
        raise Unauthorized(SIGN_IN_MESSAGE)
    return principal


def issue_session(services: Services, workspace: str, actor_id: str) -> str:
    token = secrets.token_hex(32)
    with services.db.write() as session:
        repo = SessionRepository(session)
        repo.purge_expired()
        repo.create(token, workspace, actor_id, services.settings.governance_session_hours)
    return token


def set_session_cookie(response: Response, token: str, secure: bool = False) -> None:
    # ``secure``: staging/production are served over HTTPS only.
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", path="/", secure=secure)


def ensure_local_session(services: Services, request: Request, response: Response) -> None:
    """Local-preview sign-in performed when the app page is opened."""
    if not services.settings.local_preview or current_principal(services, request):
        return
    token = issue_session(services, services.settings.governance_workspace_id, BUILTIN_OWNER["id"])
    set_session_cookie(response, token, services.settings.secure_cookies)


def switch_profile(services: Services, request: Request, requested) -> str:
    """``Preview as``: issue a new session for another active profile of the same workspace."""
    if not services.settings.local_preview:
        raise NotFound("Not found.")
    principal = require_principal(services, request)
    if requested != "owner":
        with services.db.read() as session:
            member = RecordRepository(session, principal.workspace).get(requested)
        if member is None or member.get("kind") != "member" or member.get("active") is False:
            raise Forbidden("Unknown local profile")
    return issue_session(services, principal.workspace, requested)
