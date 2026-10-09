"""Loopback request guard (ported from ``local/server.ts``).

* Host header must be ``127.0.0.1:<port>`` (DNS-rebinding protection) or an explicitly allowed host.
* A cross-site ``Origin`` header is rejected.
* Request bodies are limited to 12 MB.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.errors import PayloadTooLarge

HEALTH_PATHS = ("/health", "/ready")

MAX_BODY_BYTES = 12 * 1024 * 1024


async def _plain(send: Send, status: int, body: str) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": body.encode()})


class LocalSecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in HEALTH_PATHS and scope["method"] in ("GET", "HEAD"):
            # Probes come from load balancers with their own Host header; they expose nothing.
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        server = scope.get("server") or (self.settings.host, self.settings.port)
        allowed = {f"{self.settings.host}:{server[1]}"} | self.settings.extra_hosts
        if host not in allowed:
            await _plain(send, 403, "Host not allowed")
            return
        origin = headers.get("origin")
        # Same host over http (local) or https (behind a TLS proxy) is the same site.
        if origin and origin not in (f"http://{host}", f"https://{host}"):
            await _plain(send, 403, "Origin not allowed")
            return
        length = headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            await _plain(send, 413, "Upload limit exceeded")
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise PayloadTooLarge("Upload limit exceeded")
            return message

        await self.app(scope, limited_receive, send)
