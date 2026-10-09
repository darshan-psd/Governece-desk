"""``/api/v2-files`` (document upload/download) and ``/api/v2-import`` (trial-balance CSV)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.api.deps import context_of, json_response, services_of
from app.services import documents_service
from app.services.documents_service import IncomingFile

router = APIRouter()


async def _read_form(request: Request) -> tuple[dict, IncomingFile | None]:
    form = await request.form()
    fields: dict = {}
    incoming = None
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            if key == "file" and incoming is None:
                incoming = IncomingFile(name=value.filename or "", content=await value.read())
        else:
            fields.setdefault(key, value)
    return fields, incoming


@router.post("/api/v2-files")
async def upload(request: Request):
    ctx = await context_of(request)
    fields, incoming = await _read_form(request)
    result = await run_in_threadpool(documents_service.upload_document, services_of(request), ctx, fields, incoming)
    return json_response(result)


@router.get("/api/v2-files")
async def download(request: Request):
    ctx = await context_of(request)
    doc, content = await run_in_threadpool(documents_service.download_document, services_of(request), ctx, request.query_params.get("id"))
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.get('name') or 'document', safe='-_.!~*()' + chr(39))}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@router.post("/api/v2-import")
async def import_trial_balance(request: Request):
    ctx = await context_of(request)
    fields, incoming = await _read_form(request)
    result = await run_in_threadpool(documents_service.import_trial_balance, services_of(request), ctx, fields, incoming)
    return json_response(result)
