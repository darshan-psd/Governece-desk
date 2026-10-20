"""Core workspace records: state, create, edit, workflow transitions, comments, team profiles.

Ported from ``lib/phase-service.ts`` (``readState`` and the create/edit/transition/comment/member/
initialise actions).
"""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import bindparam
from sqlalchemy import text as sql_text

from app.core.container import Services
from app.core.errors import BadRequest, Forbidden
from app.database.dialect import POSTGRES
from app.core.jsvalues import as_list, now_iso, prop, text, today, truthy
from app.domain.portal_domain import check_company_duplicates
from app.domain.workflow_rules import (
    CANCELLED, EDIT_ROLES, EVENT_TEMPLATES, REVIEW_ROLES, can_access, review_problems, validate_record,
)
from app.permissions import Context, accessible, authorize, client_safe, prefetch_briefing_sources
from app.models import ASSIGNABLE_ROLES, Record
from app.repositories.identity import IdentityRepository
from app.repositories.records import ORDER, AuditRepository, RecordRepository, field
from app.schemas import Page, RecordQuery
from app.services.context import audit_label, save, visibility_filters
from app.services.identity_service import add_member, apply_new_credentials, prepare_new_credentials

DEMO_PROFILES = [
    {"id": "owner", "name": "Workspace owner", "role": "Owner", "companies": ["*"]},
    {"id": "secretary", "name": "Priya Sharma", "role": "Secretary", "companies": ["*"]},
    {"id": "accountant", "name": "Alex Morgan", "role": "Accountant", "companies": ["*"]},
    {"id": "director", "name": "Daniel Reed", "role": "Director", "companies": ["*"]},
    {"id": "client", "name": "Northstar client", "role": "Client", "companies": ["c1"]},
    {"id": "auditor", "name": "External auditor", "role": "Auditor", "companies": ["*"]},
]
# ``Cancelled`` ends the lifecycle of work that will not be carried out. It is offered from every
# status up to and including review, but not from ``Approved`` or ``Completed``: approved work
# carries an independent approval and completed work is finished, so either must first be returned
# to preparation (an existing, audited transition) before it can be cancelled. Nothing leads out of
# ``Cancelled`` — like a voided invoice, a cancelled record stays as history and the work is
# re-created as a new record if it turns out to be needed after all.
TRANSITIONS = {
    "Not started": ["In progress", CANCELLED],
    "In progress": ["Awaiting information", "Under review", CANCELLED],
    "Awaiting information": ["In progress", CANCELLED],
    "Under review": ["In progress", "Approved", CANCELLED],
    "Approved": ["Completed", "In progress"],
    "Completed": ["In progress"],
    CANCELLED: [],
}


def integrations(services: Services) -> list[dict]:
    s = services.settings
    return [
        {"id": "email", "name": "Email reminders", "detail": "Resend · outbound messages require explicit dispatch", "configured": bool(s.resend_api_key.get_secret_value() and s.mail_from)},
        {"id": "ai", "name": "AI drafting", "detail": "OpenAI · reviewed drafts with source references", "configured": bool(s.openai_api_key.get_secret_value() and s.openai_model)},
        {"id": "filing", "name": "Regulatory filing", "detail": "Manual submission and acknowledgement tracking", "configured": False},
        {"id": "signature", "name": "Electronic signatures", "detail": "Provider connection required; internal approvals available", "configured": False},
        {"id": "accounting", "name": "Accounting software", "detail": "Validated CSV imports available; live sync not connected", "configured": False},
        {"id": "scanning", "name": "Document malware scan", "detail": "Not connected; uploads are attachment-only", "configured": False},
    ]


def _companies(member: dict) -> list:
    return as_list(member.get("companies"))


def _visible_member(member: dict, actor_companies: list) -> dict:
    """Team & Access (role, full company-access) is Owner-only; other roles still need enough of
    this record to resolve assignee names and to filter who can be assigned within companies they
    themselves can already see."""
    companies = _companies(member)
    out = {k: v for k, v in member.items() if k != "role"}
    if "*" not in actor_companies:
        out["companies"] = actor_companies if "*" in companies else [c for c in companies if c in actor_companies]
    return out


def _state_meta(services: Services, ctx: Context) -> dict:
    """Everything in the workspace state except ``records`` (same keys and order as before)."""
    actor_companies = _companies(ctx.actor)
    is_client = ctx.role == "Client"
    is_owner = ctx.role == "Owner"
    members = [] if is_client else [
        {**(r if is_owner else _visible_member(r, actor_companies)), "client": r.get("role") == "Client"}
        for r in ctx.query(kinds=["member"])
        if (
            is_owner or "*" in actor_companies or "*" in _companies(r)
            or any(c in actor_companies for c in _companies(r))
        )
    ]
    sees_audit = not is_client and (ctx.role == "Owner" or "*" in actor_companies)
    audit = []
    if sees_audit:
        with services.db.read() as session:
            audit = AuditRepository(session, ctx.owner).recent(300)
    return {
        "actor": ctx.actor,
        "members": members,
        "audit": audit,
        "connections": integrations(services),
        "local": services.settings.local_preview,
        "date": today(),
        "eventTemplates": EVENT_TEMPLATES,
    }


def read_state(services: Services, ctx: Context) -> dict:
    """Full workspace state for the React app (unchanged contract).

    SQLite selects only the records this actor can possibly see (members excluded, company scope
    applied) in the original order; ``accessible()`` then makes the exact access decision.
    """
    candidates = ctx.query(exclude_kinds=["member"], **visibility_filters(ctx))
    prefetch_briefing_sources(ctx, candidates)
    records = [client_safe(ctx, {k: v for k, v in r.items() if k != "key"}) for r in candidates if accessible(ctx, r)]
    return {"records": records, **_state_meta(services, ctx)}


def _json(value) -> str:
    # Same encoding FastAPI's JSONResponse uses.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def read_state_json(services: Services, ctx: Context) -> bytes:
    """``read_state`` rendered as JSON bytes.

    For actors who can see every company (Owner, ``*`` staff) every non-member record is visible
    except a briefing whose sources are missing, so SQLite builds each record's JSON (column
    fields merged, ``key`` removed) and Python only joins the text: no per-record parsing,
    access checks or re-serialisation. Scoped staff and clients use ``read_state``.
    """
    if ctx.scoped_companies is not None:
        return _json(read_state(services, ctx)).encode()
    rows = ctx.store.json_rows(exclude_kinds=["member"])
    briefings = {record_id: json.loads(text) for kind, record_id, text in rows if kind == "briefing"}
    prefetch_briefing_sources(ctx, briefings.values())
    parts = [text for kind, record_id, text in rows if kind != "briefing" or accessible(ctx, briefings[record_id])]
    meta = _json(_state_meta(services, ctx))
    return ('{"records":[' + ",".join(parts) + "]," + meta[1:]).encode()


def _briefing_sources_visible(ctx: Context):
    """SQL form of the briefing rule in ``accessible()``: every source must exist (and be in scope)."""
    scope = ctx.scoped_companies
    if ctx.store.db.dialect == POSTGRES:
        in_scope = "" if scope is None else (
            " AND (CASE WHEN src.kind = 'company' THEN src.id ELSE src.data ->> 'company' END) IN :briefing_scope")
        clause = sql_text(
            "(records.kind != 'briefing' OR (coalesce(jsonb_typeof(records.data -> 'sources'), 'null') IN ('null', 'array')"
            " AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(records.data -> 'sources') = 'array'"
            " THEN records.data -> 'sources' ELSE '[]'::jsonb END) AS s(value) WHERE jsonb_typeof(s.value) != 'object'"
            " OR NOT EXISTS (SELECT 1 FROM records AS src WHERE src.owner = records.owner"
            f" AND src.id = s.value ->> 'id'{in_scope}))))"
        )
    else:
        in_scope = "" if scope is None else (
            " AND (CASE WHEN src.kind = 'company' THEN src.id ELSE json_extract(src.data, '$.company') END) IN :briefing_scope")
        clause = sql_text(
            "(records.kind != 'briefing' OR (coalesce(json_type(records.data, '$.sources'), 'null') IN ('null', 'array')"
            " AND NOT EXISTS (SELECT 1 FROM json_each(records.data, '$.sources') AS s WHERE json_type(s.value) != 'object'"
            " OR NOT EXISTS (SELECT 1 FROM records AS src WHERE src.owner = records.owner"
            f" AND src.id = json_extract(s.value, '$.id'){in_scope}))))"
        )
    if scope is not None:
        clause = clause.bindparams(bindparam("briefing_scope", value=scope, expanding=True))
    return clause


def list_records(services: Services, ctx: Context, query: RecordQuery) -> dict:
    """``GET /api/v2/records``: one page of visible records; filtering, sorting and counting in SQLite."""
    filters = visibility_filters(ctx)
    extra = list(filters.pop("extra", []))
    if not query.kind or "briefing" in query.kind:  # the rule only concerns briefings
        extra.append(_briefing_sources_visible(ctx))
    where: dict = {}
    if query.company:
        where["company"] = query.company
    if query.status:
        where["status"] = query.status
    if query.assignee is not None:
        where["assignee"] = query.assignee
    if query.priority is not None:
        where["priority"] = query.priority
    if query.due_from:
        extra.append(field("due") >= query.due_from)
    if query.due_to:
        extra.append(field("due") <= query.due_to)
    criteria = {"kinds": query.kind or None, "exclude_kinds": ["member"], "where": where, "extra": extra, **filters}
    # Repeating ``updated`` in front of the default order would stop SQLite using the
    # (owner, kind, updated) index, so the default sort is expressed without duplicates.
    if query.sort == "updated":
        order_by = ORDER if query.order == "desc" else (Record.updated.asc(), *ORDER[1:])
    else:
        column = field(query.sort)
        if ctx.store.db.dialect == POSTGRES:
            # SQLite's text order is bytewise with NULLs lowest; PostgreSQL is told the same.
            column = column.collate("C")
            order_by = (column.desc().nulls_last() if query.order == "desc" else column.asc().nulls_first(), *ORDER)
        else:
            order_by = (column.desc() if query.order == "desc" else column.asc(), *ORDER)
    rows = ctx.query(order_by=order_by, limit=query.limit, offset=query.offset, **criteria)
    prefetch_briefing_sources(ctx, rows)
    items = [client_safe(ctx, {k: v for k, v in r.items() if k != "key"}) for r in rows if accessible(ctx, r)]
    return {"items": items, "total": ctx.store.count(**criteria), "limit": query.limit, "offset": query.offset}


def audit_page(services: Services, ctx: Context, page: Page) -> dict:
    """``GET /api/v2/audit``: the full audit trail, newest first (state only carries the latest 300)."""
    if ctx.role == "Client" or ctx.scoped_companies is not None:
        raise Forbidden("Your role does not permit this action.")
    with services.db.read() as session:
        repo = AuditRepository(session, ctx.owner)
        return {"items": repo.recent(page.limit, page.offset), "total": repo.count(), "limit": page.limit, "offset": page.offset}


def initialise(services: Services, ctx: Context, body: dict) -> dict:
    """Seeds the local demo team profiles the frontend expects on first launch (existing behaviour).

    Local preview only: a real (password) organization never receives invented team members.
    """
    authorize(ctx, ["Owner"])
    if not services.settings.local_preview:
        return {"ok": True}
    existing = ctx.find_many(m["id"] for m in DEMO_PROFILES)
    missing = [m for m in DEMO_PROFILES if m["id"] not in existing]
    if missing:
        # Company access can only reference companies that exist in this organization.
        known = ctx.find_many((c for m in missing for c in m["companies"] if c != "*"), "company")
        with services.db.write() as session:
            repo = RecordRepository(session, ctx.owner)
            identity = IdentityRepository(session)
            for member in missing:
                repo.insert(member["id"], "member", {**member, "active": True, "preview": True})
                if identity.membership_by_member(ctx.owner, member["id"]) is None:
                    companies = [c for c in member["companies"] if c == "*" or c in known]
                    add_member(session, ctx.owner, member["id"], member["name"], member["role"], companies)
            AuditRepository.of(session, ctx).add(audit_label(ctx, "Enabled phase 2 and 3"), "Local role profiles and advanced workflows")
    return {"ok": True}


def _active_member_with_access(ctx: Context, member_id, target: dict) -> bool:
    """Whether ``member_id`` can be the assignee of ``target`` (its ``kind`` and ``company``).

    A Client can never be the responsible person for internal work (task/meeting/close/event/...):
    ``accessible()`` hides every one of those kinds from a Client, so assigning one to a Client
    would leave the "responsible person" unable to even see the record. A ``request`` is the one
    kind where the assignee is routinely the Client who must respond, so it is exempt.
    """
    member = ctx.find(member_id, "member")
    if member is None or member.get("active") is False or not can_access(member, target):
        return False
    return target.get("kind") == "request" or member.get("role") != "Client"


def validate_links(ctx: Context, data: dict) -> None:
    """Linked records are fetched by primary key (batched), never by scanning the workspace."""
    company = data.get("company")
    if data.get("assignee") and not _active_member_with_access(ctx, data["assignee"], {"kind": data.get("kind"), "company": company}):
        raise BadRequest("Choose an active assignee with access to this company.")
    evidence = as_list(data.get("evidence"))
    documents = ctx.find_many(evidence, "document")
    for doc_id in evidence:
        doc = documents.get(doc_id) if isinstance(doc_id, str) else None
        if doc is None or doc.get("company") != company or truthy(doc.get("supersededBy")) or not accessible(ctx, doc):
            raise BadRequest("Evidence must be a current accessible document from this company.")
    dependencies = as_list(data.get("dependencies"))
    tasks = ctx.find_many(dependencies, "task")
    for dep_id in dependencies:
        task = tasks.get(dep_id) if isinstance(dep_id, str) else None
        if task is None or task.get("company") != company:
            raise BadRequest("Dependencies must be tasks from this company.")
    if data.get("kind") == "close":
        if not isinstance(data.get("period"), str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", data["period"], re.ASCII):
            raise BadRequest("Choose a valid accounting period.")
        if data.get("trialbalance"):
            tb = ctx.find(data["trialbalance"], "trialbalance")
            if tb is None or tb.get("company") != company or tb.get("period") != data["period"]:
                raise BadRequest("Trial balance must match the company and close period.")
    if data.get("kind") == "submission":
        task = ctx.find(data.get("task"), "task")
        if task is None or task.get("company") != company:
            raise BadRequest("Link a task from this company.")
    if data.get("kind") == "request":
        # An information request is answered in the client portal, where a client sees only the
        # requests addressed to them (``accessible()``). Addressing one to a colleague therefore
        # means the client never receives it, which is silently useless; so when the company has a
        # client who could answer, the request must go to one of them. A company with no client
        # profile yet can still have requests recorded against it.
        recipient = ctx.find(data.get("assignee"), "member") or {}
        if recipient.get("role") != "Client" and any(
            m.get("role") == "Client" and m.get("active") is not False
            and can_access(m, {"kind": "request", "company": company})
            for m in ctx.query(kinds=["member"])
        ):
            raise BadRequest("Assign this request to the client who must answer it in the portal.")
    if data.get("kind") == "rule" and ctx.role not in ("Owner", "Secretary"):
        raise Forbidden("Only owners and secretaries may prepare rules.")


def create(services: Services, ctx: Context, body: dict) -> dict:
    authorize(ctx, EDIT_ROLES)
    kind = body.get("kind")
    if kind == "rule":
        authorize(ctx, ["Owner", "Secretary"])
    data = validate_record(kind, body.get("data") if truthy(body.get("data")) else {})
    warning = None
    if kind == "company":
        warning = check_company_duplicates(ctx.query(kinds=["company"]), data)
    else:
        company = ctx.find(data.get("company"), "company")
        if company is None or not can_access(ctx.actor, company):
            raise Forbidden("Company access is required.")
    if data.get("assignee") and not _active_member_with_access(ctx, data["assignee"], {"kind": kind, "company": data.get("company")}):
        raise BadRequest("Choose an active assignee with access to this company.")
    record_id = str(uuid.uuid4())
    record = {
        **data, "kind": kind, "status": data.get("status") or "Not started", "preparer": ctx.actor["id"],
        "evidence": data["evidence"] if data.get("evidence") is not None else [], "created": now_iso(),
    }
    if kind == "event":
        record["status"] = "In progress"
    validate_links(ctx, record)
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        repo.insert(record_id, kind, record)
        if kind == "event":
            for index, title in enumerate(EVENT_TEMPLATES[data["eventType"]]):
                repo.insert(f"{record_id}-{index}", "task", {
                    "title": title, "company": data["company"], "due": data["due"], "assignee": data["assignee"],
                    "status": "Not started", "priority": data["priority"], "event": record_id,
                    "notes": "Event checklist; confirm jurisdiction-specific requirements.", "preparer": ctx.actor["id"],
                    "evidence": [], "dependencies": [f"{record_id}-{index - 1}"] if index else [],
                })
        AuditRepository.of(session, ctx).add(audit_label(ctx, f"Created {kind}"), data.get("title") or data.get("name"))
    return {"ok": True, "id": record_id, **({"warning": warning} if warning else {})}


def member(services: Services, ctx: Context, body: dict) -> dict:
    """Owner: a new team profile. In password mode, also creates its sign-in with the password
    the Owner sets and shares out of band; it is the member's real password from the first sign-in
    onward, and they can change it any time from their own account settings."""
    authorize(ctx, ["Owner"])
    name, role = text(body.get("name")), text(body.get("role"))
    if not name or role not in ASSIGNABLE_ROLES:
        raise BadRequest("Enter a name and valid role.")
    requested = as_list(body.get("companies"))
    known = ctx.find_many((c for c in requested if c != "*"), "company")
    companies = [c for c in requested if c == "*" or (isinstance(c, str) and c in known)]
    if not companies:
        raise BadRequest("Assign at least one company.")
    if role == "Client" and "*" in companies:
        raise BadRequest("Assign specific companies to a client.")
    email, password = prepare_new_credentials(services, body.get("email"), body.get("password"), body.get("confirmPassword"), name)
    member_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(member_id, "member", {
            "name": name, "role": role, "companies": companies, "active": True, "preview": services.settings.local_preview,
        })
        # The membership (role + company scope) is what authorization reads.
        membership = add_member(session, ctx.owner, member_id, name, role, companies)
        if email:
            apply_new_credentials(session, services, ctx, membership, email, password)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Created local access profile"), name)
    return {"ok": True, "id": member_id}


def _creates_cycle(ctx: Context, record_id: str, start: dict) -> bool:
    """True when ``record_id`` is reachable from ``start`` through existing dependencies.

    Walks the graph one level at a time with one batched lookup per level (no query per node).
    """
    frontier, seen = [start], set()
    while frontier:
        next_ids = []
        for node in frontier:
            if node.get("id") == record_id:
                return True
            if node.get("id") in seen:
                continue
            seen.add(node.get("id"))
            next_ids.extend(as_list(node.get("dependencies")))
        found = ctx.find_many(next_ids)
        frontier = [found[i] for i in next_ids if isinstance(i, str) and i in found]
    return False


def edit(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, EDIT_ROLES)
    if r.get("kind") not in ("company", "task", "close", "meeting", "rule", "request", "event"):
        raise BadRequest("This record has a dedicated workflow.")
    if r.get("status") == CANCELLED:
        raise BadRequest("This record was cancelled and can no longer be edited.")
    if r.get("status") in ("Approved", "Completed", "Published"):
        raise BadRequest("Reopen or revise this record before editing.")
    changes = body.get("data") if isinstance(body.get("data"), dict) else {}
    data = validate_record(r["kind"], {**r, **changes})
    validate_links(ctx, {**data, "kind": r["kind"]})
    warning = check_company_duplicates(ctx.query(kinds=["company"]), data, exclude_id=r["id"]) if r["kind"] == "company" else None
    if r["kind"] == "request":
        data["response"] = r.get("response")
        data["evidence"] = r.get("evidence")
    if data.get("company") != r.get("company") and r["kind"] != "company":
        raise BadRequest("A record cannot be moved to another company.")
    documents = ctx.find_many(as_list(data.get("evidence")), "document")
    for doc_id in as_list(data.get("evidence")):
        doc = documents.get(doc_id) if isinstance(doc_id, str) else None
        if doc is None or doc.get("company") != r.get("company") or not accessible(ctx, doc):
            raise BadRequest("Evidence must be an accessible document from this company.")
    tasks = ctx.find_many(as_list(data.get("dependencies")), "task")
    for dep_id in as_list(data.get("dependencies")):
        dep = tasks.get(dep_id) if isinstance(dep_id, str) else None
        if dep is not None and dep.get("company") != r.get("company"):
            dep = None
        if dep is None or dep_id == r["id"]:
            raise BadRequest("Choose another task from the same company as a dependency.")
        if _creates_cycle(ctx, r["id"], dep):
            raise BadRequest("Dependencies cannot form a cycle.")
    updated = {
        **r, **data,
        **({"profileStatus": "Draft"} if r["kind"] == "company" and truthy(r.get("profileStatus")) else {}),
        "status": "In progress" if r.get("status") == "Under review" else r.get("status"),
        "preparer": ctx.actor["id"],
        "approval": None,
    }
    rule_version = r.get("ruleVersion") or data.get("ruleVersion")
    if rule_version is not None:
        updated["ruleVersion"] = rule_version
    result = save(services, ctx, r, updated, "Updated preparation")
    return {**result, "warning": warning} if warning else result


def review_subset(ctx: Context, record: dict) -> list[dict]:
    """Exactly the records ``review_problems()`` can match, fetched with targeted queries.

    The rule itself (``app.domain.workflow_rules.review_problems``) is unchanged; it simply
    receives this subset instead of the whole workspace.
    """
    subset: dict[str, dict] = {}
    evidence = ctx.find_many(as_list(record.get("evidence")))
    subset.update(evidence)
    doc_ids = [i for i, d in evidence.items() if d.get("kind") == "document" and d.get("company") == record.get("company")]
    if doc_ids:  # newer versions of the evidence (superseded check); versions share their company
        subset.update((r["id"], r) for r in ctx.query(kinds=["document"], where={"company": record.get("company"), "previous": doc_ids}))
    subset.update(ctx.find_many(as_list(record.get("dependencies"))))
    if record.get("kind") == "close":
        tb = ctx.find(record.get("trialbalance"))
        if tb is not None:
            subset[tb["id"]] = tb
            # ``previousImport`` is only set on trial balances, and imports chain within one company.
            subset.update((r["id"], r) for r in ctx.query(kinds=["trialbalance"], where={"company": tb.get("company"), "previousImport": tb["id"]}))
        subset.update((r["id"], r) for r in ctx.query(kinds=["request"], where={"company": record.get("company")}))
    return list(subset.values())


def transition(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    status = text(body.get("status"))
    if r.get("kind") not in ("task", "meeting", "close", "event") or status not in TRANSITIONS.get(r.get("status"), []):
        raise BadRequest("This workflow transition is not allowed.")
    if status == "Approved":
        authorize(ctx, REVIEW_ROLES)
        problems = review_problems(r, review_subset(ctx, r), ctx.actor)
        if problems:
            raise BadRequest(" ".join(problems))
    else:
        authorize(ctx, EDIT_ROLES)
    reason = text(body.get("reason"), 2000) if status == CANCELLED else ""
    if status == CANCELLED and not reason:
        raise BadRequest("Record the reason for cancelling this work.")
    if status == "Under review":
        if not r.get("assignee"):
            raise BadRequest("Assign the responsible person before review.")
        if any(not truthy(prop(item, "done")) for item in as_list(r.get("checklist"))):
            raise BadRequest("Complete the preparation checklist before review.")
    if status == "Completed":
        if truthy(r.get("reviewRequired")):
            raise BadRequest("The evidence changed. Obtain a new approval.")
        if truthy(r.get("filing")) and not any(
            s.get("status") == "Accepted" and not truthy(s.get("reviewRequired"))
            for s in ctx.query(kinds=["submission"], where={"company": r.get("company"), "task": r["id"]})
        ):
            raise BadRequest("Record the filing acknowledgement or accepted submission first.")
        # ``event`` is only set on the checklist tasks generated for an event, in the event's company.
        if r["kind"] == "event" and any(x.get("status") not in ("Completed", CANCELLED) for x in ctx.query(kinds=["task"], where={"company": r.get("company"), "event": r["id"]})):
            raise BadRequest("Complete or cancel every event task first.")
    if status == "Approved":
        evidence = []
        for doc_id in as_list(r.get("evidence")):
            doc = ctx.find(doc_id) or {}
            entry = {"id": doc_id}
            for key in ("hash", "version"):
                if doc.get(key) is not None:
                    entry[key] = doc[key]
            evidence.append(entry)
        approval = {"actor": ctx.actor["id"], "name": ctx.actor.get("name"), "at": now_iso(), "recordRevision": r["revision"],
                    "evidence": evidence, "trialbalance": r.get("trialbalance") or None}
    elif status == "In progress":
        approval = None
    else:
        approval = r.get("approval")
    # The reason is kept on the record, as ``voidReason`` is for a voided invoice; the audit entry
    # carries the actor, the timestamp and the record (``save`` writes both in one transaction).
    cancellation = {"cancelledBy": ctx.actor["id"], "cancelledAt": now_iso(), "cancellationReason": reason} if status == CANCELLED else {}
    updated = {**r, "status": status, "reviewRequired": False, "completedAt": now_iso() if status == "Completed" else None,
               "approval": approval, **cancellation}
    return save(services, ctx, r, updated, "Status: " + status)


def comment(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    if ctx.role == "Client" and (r.get("kind") != "request" or r.get("assignee") != ctx.actor["id"]):
        raise Forbidden("Only assigned requests can be answered.")
    content = text(body.get("content"), 4000)
    if not content:
        raise BadRequest("Enter a comment.")
    comments = [*as_list(r.get("comments")), {"id": str(uuid.uuid4()), "author": ctx.actor.get("name"), "at": now_iso(), "content": content}][-200:]
    return save(services, ctx, r, {**r, "comments": comments}, "Added comment")
