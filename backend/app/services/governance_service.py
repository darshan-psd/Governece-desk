"""Board room, client requests, recurring rules, submissions and evidence packs.

Ported from the meetingAction, pack, respond, requestDecision, ruleStatus, rulePreview, generate
and submissionStatus actions of ``lib/phase-service.ts``.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from app.core.container import Services
from app.core.errors import BadRequest, Forbidden, NotFound
from app.core.jsvalues import as_list, now_iso, prop, text, today, truthy
from app.domain.workflow_rules import EDIT_ROLES, escape_html, next_date, validate_record
from app.permissions import Context, accessible, authorize
from app.repositories.records import AuditRepository, RecordRepository
from app.services.context import audit_label, save
from app.services.records_service import validate_links

# Board room -------------------------------------------------------------------------------

def meeting_action(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, EDIT_ROLES)
    if r.get("kind") != "meeting":
        raise BadRequest("Select a meeting.")
    changes = body.get("data") if isinstance(body.get("data"), dict) else {}
    data = validate_record("task", {**changes, "company": r.get("company")})
    validate_links(ctx, {**data, "kind": "task"})
    task_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(task_id, "task", {
            **data, "status": "Not started", "meeting": r["id"], "preparer": ctx.actor["id"], "evidence": [], "created": now_iso(),
        })
        AuditRepository(session, ctx.owner).add(audit_label(ctx, "Created meeting action"), data["title"])
    return {"ok": True, "id": task_id}


def pack(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    """Immutable evidence snapshot of a meeting, close or task (document hashes and versions)."""
    authorize(ctx, ["Owner", "Secretary", "Director", "Auditor"])
    if r.get("kind") not in ("meeting", "close", "task"):
        raise BadRequest("Choose a meeting, close or task.")
    evidence = as_list(r.get("evidence"))
    documents = ctx.query(kinds=["document"], ids=[i for i in evidence if isinstance(i, str)])
    snapshot = {
        "record": dict(r),
        "company": ctx.find(r.get("company")),
        "documents": [{"id": d["id"], "name": d.get("name"), "hash": d.get("hash"), "version": d.get("version") or 1} for d in documents],
        "created": now_iso(),
        "preparedBy": ctx.actor.get("name"),
    }
    pack_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(pack_id, "pack", {
            "title": f"{r.get('title')} · evidence pack", "company": r.get("company"), "snapshot": snapshot,
            "status": "Snapshot", "sourceId": r["id"],
        })
        AuditRepository(session, ctx.owner).add(audit_label(ctx, "Generated evidence pack"), r.get("title"))
    return {"ok": True, "id": pack_id}


def pack_html(ctx: Context, pack_id: str) -> str:
    record = ctx.find(pack_id, "pack")
    if record is None or not accessible(ctx, record):
        raise NotFound("Pack not found.")
    e = escape_html
    snapshot = record.get("snapshot") or {}
    r = snapshot.get("record") or {}
    company = snapshot.get("company") or {}
    checklist = "".join(f"<li>{'Complete' if truthy(prop(x, 'done')) else 'Pending'} — {e(prop(x, 'label'))}</li>" for x in as_list(r.get("checklist")))
    attendees = "".join(
        f"<li>{e(prop(x, 'name'))} · {'Present' if truthy(prop(x, 'present')) else 'Absent'} · "
        f"{'Conflict declared' if truthy(prop(x, 'conflict')) else 'No conflict recorded'}</li>"
        for x in as_list(r.get("attendees"))
    )
    approval = json.dumps(r["approval"], indent=2, ensure_ascii=False) if truthy(r.get("approval")) else "Not approved"
    documents = "".join(
        f"<tr><td>{e(d.get('name'))}</td><td>{d.get('version')}</td><td style=\"word-break:break-all\">{e(d.get('hash'))}</td></tr>"
        for d in snapshot.get("documents") or []
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{e(record.get("title"))}</title><style>body{{font:15px/1.6 system-ui;max-width:900px;margin:48px auto;color:#173044}}h1{{font-size:30px}}h2{{margin-top:30px}}pre{{white-space:pre-wrap;font:inherit}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border:1px solid #ddd;text-align:left}}small{{color:#607789}}@media print{{body{{margin:15mm}}}}</style></head><body>'
        f"<small>GOVERNANCE DESK · CONTROLLED SNAPSHOT</small><h1>{e(r.get('title'))}</h1>"
        f"<p>{e(company.get('name'))} · {e(r.get('due'))} · {e(r.get('status'))}</p>"
        f"<p>Prepared by {e(snapshot.get('preparedBy'))} at {e(snapshot.get('created'))}. Source revision {r.get('revision')}.</p>"
        f"<h2>Agenda / scope</h2><pre>{e(r.get('agenda') or r.get('notes'))}</pre>"
        f"<h2>Minutes / review notes</h2><pre>{e(r.get('minutes') or r.get('notes'))}</pre>"
        f"<h2>Preparation checklist</h2><ul>{checklist}</ul>"
        f"<h2>Attendance and conflicts</h2><ul>{attendees}</ul>"
        f"<h2>Approval</h2><pre>{e(approval)}</pre>"
        f"<h2>Evidence manifest</h2><table><tr><th>Document</th><th>Version</th><th>SHA-256</th></tr>{documents}</table>"
        "<p>Snapshot only. Download supporting documents separately. Internal approval is not an electronic signature.</p></body></html>"
    )


# Client requests -------------------------------------------------------------------------

def respond(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    if r.get("kind") != "request" or r.get("status") not in ("Open", "Returned"):
        raise BadRequest("This request is not accepting responses.")
    if ctx.role == "Client" and r.get("assignee") != ctx.actor["id"]:
        raise Forbidden("This request is not assigned to you.")
    if ctx.role == "Auditor":
        raise Forbidden("Auditors cannot submit responses.")
    response = text(body.get("response"), 10000)
    if not response:
        raise BadRequest("Provide a response.")
    return save(services, ctx, r, {**r, "response": response, "status": "Responded", "respondedBy": ctx.actor["id"], "respondedAt": now_iso()}, "Submitted response")


def request_decision(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, ["Owner", "Secretary", "Accountant"])
    status = body.get("status")
    if r.get("kind") != "request" or r.get("status") != "Responded" or status not in ("Closed", "Returned"):
        raise BadRequest("Review a submitted response first.")
    if status == "Returned" and not text(body.get("reason")):
        raise BadRequest("Explain the requested correction.")
    return save(services, ctx, r, {**r, "status": status, "reviewNote": text(body.get("reason")), "reviewedBy": ctx.actor["id"]},
                "Closed request" if status == "Closed" else "Returned request")


# Rule library ----------------------------------------------------------------------------

def plans(ctx: Context, rule: dict, horizon: int = 90) -> list[dict]:
    """Upcoming obligations for a rule within ``horizon`` days (max 36), flagging existing ones."""
    result: list[dict] = []
    until = (datetime.now(timezone.utc) + timedelta(days=horizon)).date().isoformat()
    day = today()
    offset = rule.get("offset")
    if not isinstance(offset, int):
        raise BadRequest("Deadline offset must be between 0 and 365 days.")
    for step in range(1000):
        anchor = next_date(rule.get("due"), rule.get("frequency"), step) if step else rule.get("due")
        due = (date.fromisoformat(anchor) + timedelta(days=offset)).isoformat()
        if due > until:
            break
        if due < day:
            continue
        plan_id = f"rule-{rule['id']}-{rule.get('company')}-{anchor}"
        result.append({"id": plan_id, "due": due, "anchor": anchor, "exists": False, "version": rule.get("ruleVersion")})
        if len(result) >= 36:
            break
    existing = ctx.find_many(p["id"] for p in result)  # one batched lookup instead of one per period
    for p in result:
        p["exists"] = p["id"] in existing
    return result


def rule_status(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, ["Owner", "Secretary", "Director"])
    if r.get("kind") != "rule":
        raise BadRequest("Rule not found.")
    status = text(body.get("status"))
    if status == "Under review" and r.get("status") == "Draft":
        return save(services, ctx, r, {**r, "status": status}, "Submitted rule for review")
    if status == "Published" and r.get("status") == "Under review":
        if r.get("preparer") == ctx.actor["id"]:
            raise BadRequest("A different reviewer must publish this rule.")
        if not text(r.get("source")):
            raise BadRequest("A source reference is required.")
        return save(services, ctx, r, {**r, "status": status, "publishedBy": ctx.actor["id"], "publishedAt": now_iso()}, "Published reviewed rule")
    if status == "Draft" and r.get("status") in ("Published", "Under review"):
        return save(services, ctx, r, {**r, "status": status, "preparer": ctx.actor["id"], "ruleVersion": (r.get("ruleVersion") or 1) + 1, "publishedBy": None}, "Started new rule revision")
    raise BadRequest("This rule transition is not allowed.")


def rule_preview(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, ["Owner", "Secretary", "Director"])
    if r.get("kind") != "rule":
        raise BadRequest("Select a rule.")
    return {"ok": True, "plan": plans(ctx, r)}


def generate(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    """Creates the planned tasks. Deterministic IDs + insert-or-ignore prevent duplicates."""
    authorize(ctx, ["Owner", "Secretary"])
    if r.get("kind") != "rule" or r.get("status") != "Published":
        raise BadRequest("Publish the reviewed rule first.")
    plan = [p for p in plans(ctx, r) if not p["exists"]]
    if not plan:
        return {"ok": True, "count": 0}
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        for p in plan:
            repo.insert_or_ignore(p["id"], "task", {
                "title": r.get("title"), "company": r.get("company"), "due": p["due"], "assignee": r.get("assignee"),
                "priority": r.get("priority"), "status": "Not started", "category": "Recurring", "source": r.get("source"),
                "preparer": ctx.actor["id"], "evidence": [],
                "rule": {"id": r["id"], "version": r.get("ruleVersion"), "anchor": p["anchor"], "source": r.get("source")},
            })
        AuditRepository(session, ctx.owner).add(audit_label(ctx, "Generated recurring obligations"), r.get("title"))
    return {"ok": True, "count": len(plan)}


# Submissions -----------------------------------------------------------------------------

SUBMISSION_FLOW = {"Prepared": ["Authorised"], "Authorised": ["Submitted"], "Submitted": ["Accepted", "Rejected"], "Rejected": ["Prepared"]}


def submission_status(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, ["Owner", "Secretary"])
    if r.get("kind") != "submission":
        raise BadRequest("Select a submission.")
    status = body.get("status")
    if status not in SUBMISSION_FLOW.get(r.get("status"), []):
        raise BadRequest("Invalid submission transition.")
    linked = ctx.find(r.get("task"), "task")
    if linked is not None and linked.get("company") != r.get("company"):
        linked = None
    if status in ("Authorised", "Submitted", "Accepted") and (
        linked is None or not truthy(linked.get("approval")) or truthy(linked.get("reviewRequired")) or linked.get("status") not in ("Approved", "Completed")
    ):
        raise BadRequest("Link a currently approved task before authorising.")
    reference = text(body.get("reference") if truthy(body.get("reference")) else r.get("reference"))
    if status in ("Submitted", "Accepted", "Rejected") and not reference:
        raise BadRequest("Record the portal reference or acknowledgement.")
    evidence = as_list(body.get("evidence") if truthy(body.get("evidence")) else r.get("evidence"))
    company = r.get("company")
    documents = {i: d for i, d in ctx.find_many(evidence, "document").items() if d.get("company") == company}
    if any(not isinstance(i, str) or i not in documents or truthy(documents[i].get("supersededBy")) for i in evidence):
        raise BadRequest("Use current documents from this company.")
    if status in ("Accepted", "Rejected") and not any(isinstance(i, str) and i in documents for i in evidence):
        raise BadRequest("Attach the acknowledgement document.")
    return save(services, ctx, r, {**r, "status": status, "reference": reference, "evidence": evidence, "recordedBy": ctx.actor["id"], "reviewRequired": False},
                "Submission " + str(status).lower())
