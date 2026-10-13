"""Serves the built React app (index.html, app.js, style.css) from the frontend build folder."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import services_of
from app.auth import ensure_csrf_cookie, ensure_local_session

router = APIRouter()

ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


def _asset(request: Request, path: str) -> Response:
    name, media_type = ASSETS[path]
    file = services_of(request).settings.governance_frontend_dir / name
    if not file.is_file():
        return Response("The React frontend has not been built. Run: pnpm frontend:build", status_code=503, media_type="text/plain")
    return Response(file.read_bytes(), media_type=media_type, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/")
async def index(request: Request):
    response = _asset(request, "/")
    services = services_of(request)
    # Local-preview sign-in: opening the app page creates an owner session if none exists.
    issued = await run_in_threadpool(ensure_local_session, services, request, response)
    if not issued:
        await run_in_threadpool(ensure_csrf_cookie, services, request, response)
    return response


@router.get("/app.js")
async def script(request: Request):
    return _asset(request, "/app.js")


@router.get("/style.css")
async def stylesheet(request: Request):
    return _asset(request, "/style.css")
