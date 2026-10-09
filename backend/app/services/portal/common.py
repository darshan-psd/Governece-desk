"""Helpers shared by the client-portal services (ported from ``lib/portal-service.ts``)."""
from __future__ import annotations

from typing import Any

from app.core.container import Services
from app.core.errors import BadRequest, Conflict, NotFound
from app.core.jsvalues import as_list, js_string, truthy, unique
from app.domain.portal_domain import INCORPORATION_SOURCE, STAGES
from app.domain.workflow_rules import can_access
from app.permissions import Context, authorize
from app.services.context import same_revision, save

STAFF = ["Owner", "Secretary", "Accountant"]
CHANGED = "This record changed. Reload and retry."


def writer(ctx: Context) -> None:
    authorize(ctx, STAFF)


def company(ctx: Context, company_id: Any) -> dict:
    record = ctx.find(company_id, "company")
    if record is None or not can_access(ctx.actor, record):
        raise NotFound("Company not found.")
    return record


def record(ctx: Context, body: dict, kind: str) -> dict:
    found = ctx.find(body.get("id"), kind)
    if found is None:
        raise NotFound("Record not found.")
    company(ctx, found.get("company") or found.get("id"))
    if not same_revision(body, found):
        raise Conflict(CHANGED)
    return found


def evidence(ctx: Context, ids: Any, company_id: str) -> list[str]:
    """Attachments must be current documents of this company that are published to the portal."""
    out = unique([js_string(i) for i in as_list(ids)])
    documents = ctx.find_many(out, "document")
    for doc_id in out:
        d = documents.get(doc_id)
        if d is None or d.get("company") != company_id or not truthy(d.get("shared")) or truthy(d.get("supersededBy")):
            raise BadRequest("Attach current documents shared with this company portal.")
    return out


def onboarding(company_id: str) -> dict:
    return {
        "company": company_id,
        "title": "Company registration & onboarding",
        "status": "In progress",
        "stages": [{"id": i, "title": t, "description": d, "status": "Not started", "due": "", "evidence": [], "note": ""} for i, t, d in STAGES],
        "source": INCORPORATION_SOURCE,
    }


def portal_save(services: Services, ctx: Context, r: dict, data: dict, action: str) -> dict:
    subject = r.get("title") or r.get("name") or r.get("number") or r.get("reference") or r.get("id")
    return save(services, ctx, r, data, action, subject=subject, conflict=CHANGED)
