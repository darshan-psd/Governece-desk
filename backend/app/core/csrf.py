"""CSRF check for state-changing API requests (double-submit cookie).

Every POST/PUT/PATCH/DELETE to ``/api/`` must send ``X-CSRF-Token`` equal to the CSRF cookie.
Another site can make the browser send cookies, but cannot read them or set custom headers on a
cross-site request, so it cannot produce a matching pair. For signed-in requests
``app.auth.require_principal`` additionally checks that the token belongs to the session.
"""
from __future__ import annotations

import hmac
import json
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import CSRF_HEADER, CSRF_MESSAGE, SAFE_METHODS, csrf_cookie_name
from app.core.config import Settings


def _cookie(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:  # noqa: BLE001 - a malformed cookie header simply has no token
                return None
            morsel = jar.get(name)
            if morsel is not None:
                return morsel.value
    return None


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


class CsrfMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.cookie_name = csrf_cookie_name(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS and scope["path"].startswith("/api/"):
            sent = _header(scope, CSRF_HEADER.encode())
            cookie = _cookie(scope, self.cookie_name)
            if not sent or not cookie or not hmac.compare_digest(sent, cookie):
                body = json.dumps({"error": CSRF_MESSAGE}).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
