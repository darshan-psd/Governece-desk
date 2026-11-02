"""Serves the built React app (index.html, app.js, style.css) from the frontend build folder.

Code-split chunks (``/chunks/<name>.js``, loaded on demand by the document review workspace) and the
PDF.js worker (``/pdf.worker.js``) are served from the same folder. Chunk names are validated
against a strict pattern and resolved inside ``chunks/`` only, so no other file can be requested.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import services_of
from app.auth import ensure_csrf_cookie, ensure_local_session

router = APIRouter()

ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/pdf.worker.js": ("pdf.worker.js", "text/javascript")}
# esbuild names chunks after their source module (e.g. "exceljs.min-7QX2.js"): dots allowed, ".." never.
CHUNK_NAME = re.compile(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}\.js", re.ASCII)


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


@router.get("/pdf.worker.js")
async def pdf_worker(request: Request):
    return _asset(request, "/pdf.worker.js")


@router.get("/chunks/{name}")
async def chunk(request: Request, name: str):
    if not CHUNK_NAME.fullmatch(name):
        return Response("Not found", status_code=404, media_type="text/plain")
    file = services_of(request).settings.governance_frontend_dir / "chunks" / name
    if not file.is_file():
        return Response("Not found", status_code=404, media_type="text/plain")
    # Chunk file names carry a content hash, so a cached copy can never be stale.
    return Response(file.read_bytes(), media_type="text/javascript",
                    headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
