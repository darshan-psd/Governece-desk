"""``/api/v2``: internal workspace state, actions, CSV report and evidence-pack exports."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import NO_STORE, attachment, context_of, json_body, json_response, services_of
from app.schemas import Page, RecordQuery, parse_query
from app.services import governance_service, intelligence_service, records_service
from app.services.dispatch import workspace_mutate

router = APIRouter()


@router.get("/api/v2")
async def workspace_state(request: Request):
    ctx = await context_of(request)
    params = request.query_params
    if "export" in params:
        pack_id = params.get("id")
        if pack_id:
            html = await run_in_threadpool(governance_service.pack_html, ctx, pack_id)
            return attachment(html, "text/html; charset=utf-8", "governance-evidence-pack.html")
        csv = await run_in_threadpool(intelligence_service.report_csv, ctx)
        return attachment(csv, "text/csv", "governance-report.csv")
    body = await run_in_threadpool(records_service.read_state_json, services_of(request), ctx)
    return Response(body, media_type="application/json", headers=NO_STORE)


@router.get("/api/v2/records")
async def list_records(request: Request):
    """Paginated, filtered records (new; additive). The React app still uses ``GET /api/v2``."""
    ctx = await context_of(request)
    query = parse_query(RecordQuery, request.query_params)
    return json_response(await run_in_threadpool(records_service.list_records, services_of(request), ctx, query), no_store=True)


@router.get("/api/v2/audit")
async def audit(request: Request):
    """Paginated audit trail, newest first (new; additive)."""
    ctx = await context_of(request)
    page = parse_query(Page, request.query_params)
    return json_response(await run_in_threadpool(records_service.audit_page, services_of(request), ctx, page), no_store=True)


@router.post("/api/v2")
async def workspace_action(request: Request):
    ctx = await context_of(request)
    body = await json_body(request)
    return json_response(await run_in_threadpool(workspace_mutate, services_of(request), ctx, body))
