"""``/api/auth/*``: sign-in, sign-out, sessions, password reset, activation and team accounts."""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import json_body, json_response, services_of
from app.auth import (
    client_address, clear_session_cookie, ensure_csrf_cookie, require_principal, set_session_cookie,
)
from app.core.errors import NotFound
from app.schemas import Page, parse_query
from app.services import auth_service
from app.services.context import load_context

router = APIRouter()


def _client(request: Request) -> str:
    return client_address(services_of(request).settings, request)


def _signed_in(request: Request, allow_pending: bool = False):
    """``allow_pending``: True for the few endpoints usable while a forced password change is
    pending (checking who's signed in, changing the password itself, signing out)."""
    services = services_of(request)
    principal = require_principal(services, request)
    return services, principal, load_context(services, principal, require_password_set=not allow_pending)


@router.get("/api/auth/config")
async def config(request: Request):
    """Public: which sign-in the page should show. Also hands out the CSRF cookie."""
    services = services_of(request)
    response = json_response({"mode": services.settings.governance_auth_mode}, no_store=True)
    await run_in_threadpool(ensure_csrf_cookie, services, request, response)
    return response


@router.get("/api/auth/me")
async def me(request: Request):
    def run():
        services, principal, ctx = _signed_in(request, allow_pending=True)
        return auth_service.me(services, ctx, principal)
    response = json_response(await run_in_threadpool(run), no_store=True)
    await run_in_threadpool(ensure_csrf_cookie, services_of(request), request, response)
    return response


@router.post("/api/auth/login")
async def login(request: Request):
    services = services_of(request)
    body = await json_body(request)
    result = await run_in_threadpool(auth_service.sign_in, services, body.get("email"), body.get("password"),
                                     body.get("organization"), _client(request), request.headers.get("user-agent"))
    if result.token is None:
        return json_response({"ok": False, "chooseOrganization": result.organizations}, no_store=True)
    response = json_response({"ok": True}, no_store=True)
    set_session_cookie(response, services.settings, result.token)
    return response


@router.post("/api/auth/logout")
async def logout(request: Request):
    body = await json_body(request)

    def run():
        services, principal, ctx = _signed_in(request, allow_pending=True)
        return auth_service.sign_out(services, ctx, principal, body.get("everywhere") is True, _client(request))
    count = await run_in_threadpool(run)
    response = json_response({"ok": True, "sessionsEnded": count}, no_store=True)
    clear_session_cookie(response, services_of(request).settings)
    return response


@router.get("/api/auth/sessions")
async def sessions(request: Request):
    def run():
        services, principal, _ctx = _signed_in(request)
        return auth_service.list_sessions(services, principal)
    return json_response({"items": await run_in_threadpool(run)}, no_store=True)


@router.post("/api/auth/sessions/revoke")
async def revoke(request: Request):
    body = await json_body(request)

    def run():
        services, principal, ctx = _signed_in(request)
        auth_service.revoke_session(services, ctx, principal, body.get("id"), _client(request))
    await run_in_threadpool(run)
    return json_response({"ok": True}, no_store=True)


@router.post("/api/auth/password/change")
async def change_password(request: Request):
    body = await json_body(request)

    def run():
        services, principal, ctx = _signed_in(request, allow_pending=True)
        auth_service.change_password(services, ctx, principal, body.get("current"), body.get("password"), _client(request))
    await run_in_threadpool(run)
    return json_response({"ok": True}, no_store=True)


@router.post("/api/auth/password/forgot")
async def forgot_password(request: Request):
    body = await json_body(request)
    await run_in_threadpool(auth_service.request_password_reset, services_of(request), body.get("email"), _client(request))
    return json_response({"ok": True, "message": "If the email belongs to an active account, a reset link has been sent."}, no_store=True)


@router.post("/api/auth/password/reset")
async def reset_password(request: Request):
    body = await json_body(request)
    await run_in_threadpool(auth_service.complete_password_reset, services_of(request), body.get("token"), body.get("password"), _client(request))
    response = json_response({"ok": True}, no_store=True)
    clear_session_cookie(response, services_of(request).settings)
    return response


@router.post("/api/auth/activate")
async def activate(request: Request):
    body = await json_body(request)
    await run_in_threadpool(auth_service.activate_account, services_of(request), body.get("token"), body.get("password"), _client(request))
    return json_response({"ok": True}, no_store=True)


# Team accounts (Owner) ------------------------------------------------------------------------

@router.get("/api/auth/team")
async def team(request: Request):
    def run():
        services, _principal, ctx = _signed_in(request)
        return auth_service.team_accounts(services, ctx)
    return json_response({"items": await run_in_threadpool(run)}, no_store=True)


@router.post("/api/auth/team/invite")
async def invite(request: Request):
    body = await json_body(request)

    def run():
        services, _principal, ctx = _signed_in(request)
        return auth_service.invite_member(services, ctx, body.get("member"), body.get("email"), _client(request))
    return json_response(await run_in_threadpool(run), no_store=True)


@router.post("/api/auth/team/status")
async def member_status(request: Request):
    body = await json_body(request)

    def run():
        services, _principal, ctx = _signed_in(request)
        return auth_service.set_member_status(services, ctx, body.get("member"), body.get("status"), _client(request))
    return json_response(await run_in_threadpool(run), no_store=True)


@router.post("/api/auth/team/companies")
async def member_companies(request: Request):
    body = await json_body(request)

    def run():
        services, _principal, ctx = _signed_in(request)
        return auth_service.set_member_companies(services, ctx, body.get("member"), body.get("companies"), _client(request))
    return json_response(await run_in_threadpool(run), no_store=True)


@router.get("/api/auth/security-events")
async def security_events(request: Request):
    page = parse_query(Page, request.query_params)

    def run():
        services, _principal, ctx = _signed_in(request)
        return auth_service.security_events(services, ctx, page.limit, page.offset)
    return json_response(await run_in_threadpool(run), no_store=True)


@router.api_route("/api/auth/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def unknown(rest: str):
    raise NotFound("Not found.")
