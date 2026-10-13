"""Client onboarding, company profiles, compliance calendar, threads and portal documents.

Ported from the corresponding ``portalMutate`` actions in ``lib/portal-service.ts``.
"""
from __future__ import annotations

import re
import uuid

from app.core.container import Services
from app.core.errors import BadRequest, Conflict, Forbidden
from app.core.jsvalues import as_list, js_number, now_iso, text, truthy
from app.domain.portal_domain import ACT_SOURCE, ANNUAL_ITEMS, DOCUMENT_CATEGORIES, company_profile
from app.domain.workflow_rules import require_date
from app.permissions import Context
from app.repositories.records import AuditRepository, RecordRepository
from app.repositories.identity import IdentityRepository
from app.services.context import audit_label
from app.services.identity_service import add_member, apply_new_credentials, prepare_new_credentials
from app.services.portal.common import evidence, onboarding, portal_save, record, writer

STAGE_STATUSES = ("Not started", "In progress", "Waiting for client", "Verified")
OBLIGATION_STATUSES = ("Not started", "In progress", "Waiting for client", "Completed", "Not applicable")


# Company profile -------------------------------------------------------------------------

def company_create(services: Services, ctx: Context, body: dict) -> dict:
    """Staff may optionally onboard the client's sign-in in the same step: a name alone creates a
    credential-less profile (invite it later from Team & Access); a name with an email and
    password creates and credentials it immediately — the client can sign in right away with no
    forced change. Either way this is one database transaction with the company itself, so a
    rejected password or a taken email leaves nothing behind — not a company with half-created
    client access.
    """
    client_name = text(body.get("clientName"))
    client_email = client_password = None
    if ctx.role != "Client" and client_name and truthy(body.get("clientEmail")):
        client_email, client_password = prepare_new_credentials(
            services, body.get("clientEmail"), body.get("clientPassword"), body.get("clientConfirmPassword"), client_name)
    profile = company_profile(body.get("data"))
    company_id = str(uuid.uuid4())
    company = {**profile, "profileStatus": "Draft", "portalCreatedBy": ctx.actor["id"], "created": now_iso()}
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        if ctx.role == "Client":
            # A client gains access to the company it proposes by extending its own profile.
            member = ctx.find(ctx.actor["id"], "member")
            if member is None:
                raise Forbidden("A client profile is required.")
            companies = [*as_list(member.get("companies")), company_id]
            if not repo.update_if_revision(member["id"], member["revision"], {**member, "companies": companies}):
                raise Conflict("Your profile changed. Retry company creation.")
        repo.insert(company_id, "company", company)
        repo.insert(f"onboard-{company_id}", "onboarding", onboarding(company_id))
        if ctx.role == "Client":
            IdentityRepository(session).grant_company(ctx.owner, ctx.membership_id, company_id)
        if ctx.role != "Client" and client_name:
            client_id = str(uuid.uuid4())
            repo.insert(client_id, "member", {"name": client_name, "role": "Client", "companies": [company_id], "active": True, "preview": services.settings.local_preview})
            membership = add_member(session, ctx.owner, client_id, client_name, "Client", [company_id])
            if client_email:
                apply_new_credentials(session, services, ctx, membership, client_email, client_password)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Started client onboarding"), company["name"])
    return {"ok": True, "id": company_id}


def company_save(services: Services, ctx: Context, body: dict, submit: bool) -> dict:
    c = record(ctx, body, "company")
    data = company_profile(body.get("data"), submit)
    return portal_save(services, ctx, c, {
        **c, **data, "profileStatus": "Submitted" if submit else "Draft", "profileReviewedBy": None, "profileNote": "", "submittedBy": ctx.actor["id"],
    }, "Submitted company profile" if submit else "Updated company profile")


def company_review(services: Services, ctx: Context, body: dict) -> dict:
    writer(ctx)
    c = record(ctx, body, "company")
    if c.get("profileStatus") != "Submitted":
        raise BadRequest("Submit the company profile before review.")
    status = body.get("status")
    if status not in ("Verified", "Needs changes"):
        raise BadRequest("Choose a review decision.")
    if status == "Needs changes" and not text(body.get("note")):
        raise BadRequest("Explain the information required.")
    return portal_save(services, ctx, c, {**c, "profileStatus": status, "profileNote": text(body.get("note"), 2000), "profileReviewedBy": ctx.actor["id"]},
                       "Company profile " + status.lower())


# Onboarding stages -----------------------------------------------------------------------

def start_onboarding(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    if ctx.find(f"onboard-{c['id']}") is not None:
        raise BadRequest("Onboarding already exists.")
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(f"onboard-{c['id']}", "onboarding", onboarding(c["id"]))
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Started onboarding"), c.get("name"))
    return {"ok": True}


def stage_update(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    """Stages are verified strictly in order, each with current published evidence."""
    writer(ctx)
    cid = c["id"]
    r = record(ctx, body, "onboarding")
    stages = r.get("stages") or []
    index = next((i for i, s in enumerate(stages) if s.get("id") == body.get("stage")), -1)
    if index < 0:
        raise BadRequest("Stage not found.")
    status = text(body.get("status"))
    if status not in STAGE_STATUSES:
        raise BadRequest("Choose a stage status.")
    if truthy(body.get("due")):
        require_date(body.get("due"))
    ids = evidence(ctx, body.get("evidence"), cid)
    if status == "Verified":
        superseded = lambda s: any(truthy((ctx.find(i) or {}).get("supersededBy")) for i in s.get("evidence") or [])  # noqa: E731
        if index > 0 and any(s.get("status") != "Verified" or superseded(s) for s in stages[:index]):
            raise BadRequest("Verify the earlier onboarding stages with current evidence first.")
        if not ids:
            raise BadRequest("Attach evidence before verifying a stage.")
        if index == 0 and c.get("profileStatus") != "Verified":
            raise BadRequest("Verify the submitted company profile first.")
        if body.get("stage") == "registration" and (not c.get("registration") or not c.get("incorporationDate")):
            raise BadRequest("Record the registration identifier and incorporation date first.")
        if body.get("stage") == "annual" and not any(truthy(x.get("due")) for x in ctx.query(kinds=["portalobligation"], where={"company": cid})):
            raise BadRequest("Schedule the annual compliance plan before handover.")
    updated = []
    for i, s in enumerate(stages):
        if i == index:
            updated.append({**s, "status": status, "due": text(body.get("due")), "evidence": ids, "note": text(body.get("note"), 2000),
                            "reviewedBy": ctx.actor.get("name"), "updated": now_iso()})
        elif i > index and status != "Verified" and s.get("status") == "Verified":
            # Reopening a stage reopens every later verified stage.
            updated.append({**s, "status": "In progress"})
        else:
            updated.append(s)
    complete = all(s.get("status") == "Verified" for s in updated)
    return portal_save(services, ctx, r, {**r, "stages": updated, "status": "Completed" if complete else "In progress"}, "Updated onboarding stage")


# Compliance calendar ---------------------------------------------------------------------

def annual_plan(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    period = text(body.get("period"))
    if not re.fullmatch(r"20\d{2}-\d{2}", period, re.ASCII) or js_number(period[-2:]) != (js_number(period[:4]) + 1) % 100:
        raise BadRequest("Use a financial year such as 2026-27.")
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        for i, title in enumerate(ANNUAL_ITEMS):
            # Deterministic IDs make the checklist idempotent per company and financial year.
            repo.insert_or_ignore(f"annual-{c['id']}-{period}-{i}", "portalobligation", {
                "company": c["id"], "title": title, "period": period, "category": "Annual compliance", "due": "",
                "status": "Needs scheduling", "source": ACT_SOURCE, "note": "Applicability and due date require secretary review.",
                "evidence": [], "filing": i < 2,
            })
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Created annual compliance checklist"), f"{c.get('name')} {period}")
    return {"ok": True}


def obligation_upsert(services: Services, ctx: Context, body: dict, c: dict, existing: bool) -> dict:
    writer(ctx)
    r = record(ctx, body, "portalobligation") if existing else None
    if r and truthy(r.get("sourceTask")):
        raise BadRequest("Update this linked item in the internal work register.")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    title, source, due = text(data.get("title")), text(data.get("source")), text(data.get("due"))
    status = text(data.get("status")) or "Not started"
    if not title or not source or not due:
        raise BadRequest("Enter a title, reviewed due date and source or deadline basis.")
    require_date(due)
    if status not in OBLIGATION_STATUSES:
        raise BadRequest("Choose a compliance status.")
    ids = evidence(ctx, data.get("evidence"), c["id"])
    if status == "Completed" and (not ids or (truthy(data.get("filing")) and not text(data.get("reference")))):
        raise BadRequest("Completion requires evidence and a filing acknowledgement reference when filing applies.")
    if status == "Not applicable" and not text(data.get("note")):
        raise BadRequest("Explain why this item does not apply.")
    value = {
        **(r or {}), "company": c["id"], "title": title, "due": due, "source": source, "status": status,
        "period": text(data.get("period")), "category": text(data.get("category")) or "Compliance",
        "note": text(data.get("note"), 2000), "filing": truthy(data.get("filing")), "reference": text(data.get("reference")), "evidence": ids,
    }
    if r:
        return portal_save(services, ctx, r, value, "Updated client compliance")
    obligation_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(obligation_id, "portalobligation", value)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Scheduled client compliance"), title)
    return {"ok": True, "id": obligation_id}


def publish_task(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    task = ctx.find(body.get("task"), "task")
    if task is None or task.get("company") != c["id"]:
        raise BadRequest("Select a task from this company.")
    # ``sourceTask`` is only set on portal obligations, published in the task's own company.
    if ctx.exists(kinds=["portalobligation"], where={"company": c["id"], "sourceTask": task["id"]}):
        raise BadRequest("This task is already in the portal.")
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(f"portal-{task['id']}", "portalobligation", {
            "company": c["id"], "title": task.get("title"), "sourceTask": task["id"], "due": task.get("due"), "status": task.get("status"),
            "source": task.get("source") or "Company secretary work register", "note": text(body.get("note")), "evidence": [],
        })
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Published work to client calendar"), task.get("title"))
    return {"ok": True}


# Conversations ---------------------------------------------------------------------------

def _message(ctx: Context, content: str, attachments: list[str]) -> dict:
    return {"id": str(uuid.uuid4()), "by": ctx.actor["id"], "author": ctx.actor.get("name"), "role": ctx.role,
            "content": content, "attachments": attachments, "at": now_iso()}


def thread_create(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    subject, content = text(body.get("subject")), text(body.get("content"), 10000)
    if not subject or not content:
        raise BadRequest("Enter a subject and message.")
    ids = evidence(ctx, body.get("attachments"), c["id"])
    thread_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(thread_id, "thread", {
            "company": c["id"], "title": subject, "status": "Open", "messages": [_message(ctx, content, ids)],
            "readBy": {ctx.actor["id"]: now_iso()},
        })
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Started portal conversation"), subject)
    return {"ok": True, "id": thread_id}


def thread_action(services: Services, ctx: Context, body: dict, c: dict, action: str) -> dict:
    r = record(ctx, body, "thread")
    read_by = {**(r.get("readBy") or {}), ctx.actor["id"]: now_iso()}
    if action == "threadRead":
        return portal_save(services, ctx, r, {**r, "readBy": read_by}, "Read portal conversation")
    if action == "threadStatus":
        writer(ctx)
        status = body.get("status")
        if status not in ("Open", "Resolved"):
            raise BadRequest("Choose Open or Resolved.")
        return portal_save(services, ctx, r, {**r, "status": status}, "Conversation " + status.lower())
    if r.get("status") != "Open":
        raise BadRequest("Reopen the conversation before replying.")
    content = text(body.get("content"), 10000)
    if not content:
        raise BadRequest("Enter a message.")
    messages = r.get("messages") or []
    if len(messages) >= 500:
        raise BadRequest("Start a new conversation; this thread has reached 500 messages.")
    message = _message(ctx, content, evidence(ctx, body.get("attachments"), c["id"]))
    return portal_save(services, ctx, r, {**r, "messages": [*messages, message], "readBy": read_by}, "Sent portal message")


# Portal documents ------------------------------------------------------------------------

def document_publish(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    r = record(ctx, body, "document")
    category = text(body.get("category"))
    if category not in DOCUMENT_CATEGORIES:
        raise BadRequest("Choose a document category.")
    shared = truthy(body.get("shared"))
    return portal_save(services, ctx, r, {**r, "category": category, "period": text(body.get("period")), "shared": shared},
                       "Published portal document" if shared else "Updated document visibility")
