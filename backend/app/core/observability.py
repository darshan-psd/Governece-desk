"""Outermost HTTP middleware: request IDs, request logging, security headers, last-resort 500s.

Pure ASGI (no BaseHTTPMiddleware) so streaming and exception behaviour stay unchanged.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.logging import request_id_var

log = logging.getLogger("governance.http")
REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
GENERIC_ERROR = {"error": "Unable to complete the local request."}

# The React app needs only its own script, its own stylesheet and same-origin fetches: no inline
# scripts, eval, external fonts or images. Its two `style={...}` props are applied by React through
# the DOM style API, which CSP does not restrict, so no 'unsafe-inline' is needed (verified by the
# browser tests, which fail on any CSP violation).
APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI (development only) loads its assets from jsDelivr and runs an inline bootstrap script.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
DOCS_PATHS = ("/api/docs", "/api/docs/oauth2-redirect")


def security_headers(settings: Settings, path: str) -> list[tuple[bytes, bytes]]:
    csp = settings.governance_csp or (DOCS_CSP if path in DOCS_PATHS else APP_CSP)
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"x-frame-options", b"DENY"),
        (b"content-security-policy", csp.encode()),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    ]
    if settings.hsts_enabled:
        headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    return headers


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False
        extra_headers = [*security_headers(self.settings, scope["path"]), (b"x-request-id", request_id.encode())]

        async def send_with_headers(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                present = {name.lower() for name, _ in message.get("headers", [])}
                message["headers"] = [*message.get("headers", []), *[(n, v) for n, v in extra_headers if n not in present]]
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:  # noqa: BLE001 - last resort: log with the request ID, answer with the generic 500
            log.exception("Unhandled error", extra={"event": "unhandled_error", "method": scope["method"], "path": scope["path"]})
            if response_started:
                raise
            body = json.dumps(GENERIC_ERROR).encode()
            await send_with_headers({"type": "http.response.start", "status": 500,
                                     "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
        finally:
            log.info("%s %s %s", scope["method"], scope["path"], status, extra={
                "event": "http_request", "method": scope["method"], "path": scope["path"],
                "status": status, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            })
            request_id_var.reset(token)
