"""Action dispatch for ``POST /api/v2`` and ``POST /api/portal``.

The frontend sends ``{"action": "...", ...}`` bodies. The dispatch order matches the Node
backend exactly (which checks run before the target record is resolved), so status codes and
error messages stay compatible.
"""
from __future__ import annotations

from typing import Callable

from app.core.container import Services
from app.core.errors import BadRequest, Forbidden, NotFound
from app.core.jsvalues import text, truthy
from app.permissions import Context, authorize
from app.services import calendar_service, documents_service, governance_service, intelligence_service, records_service
from app.services.context import target
from app.services.portal import billing, onboarding
from app.services.portal.common import STAFF, company

# Actions that do not operate on an existing record.
WORKSPACE_ACTIONS: dict[str, Callable] = {
    "initialise": records_service.initialise,
    "create": records_service.create,
    "member": records_service.member,
    "automation": intelligence_service.automation,
    "briefing": intelligence_service.briefing,
    "templateCreate": calendar_service.template_create,
    "calendarPreview": calendar_service.calendar_preview,
    "calendarApply": calendar_service.calendar_apply,
}
# Actions on the record identified by ``id`` + ``revision``.
RECORD_ACTIONS: dict[str, Callable] = {
    "meetingAction": governance_service.meeting_action,
    "comment": records_service.comment,
    "respond": governance_service.respond,
    "requestDecision": governance_service.request_decision,
    "requestNotes": governance_service.request_notes,
    "edit": records_service.edit,
    "transition": records_service.transition,
    "ruleStatus": governance_service.rule_status,
    "rulePreview": governance_service.rule_preview,
    "generate": governance_service.generate,
    "reviewImport": documents_service.review_import,
    "documentShare": documents_service.document_share,
    "submissionStatus": governance_service.submission_status,
    "pack": governance_service.pack,
    "noticeRead": intelligence_service.notice_read,
    "dispatchEmail": intelligence_service.dispatch_email,
    "templateSave": calendar_service.template_save,
    "templateDuplicate": calendar_service.template_duplicate,
    "templateArchive": calendar_service.template_archive,
    "confirmDue": calendar_service.confirm_due,
    "companyStatus": calendar_service.company_status,
}


def workspace_mutate(services: Services, ctx: Context, body: dict) -> dict:
    action = text(body.get("action"))
    if action in WORKSPACE_ACTIONS:
        return WORKSPACE_ACTIONS[action](services, ctx, body)
    record = target(ctx, body)
    handler = RECORD_ACTIONS.get(action)
    if handler is None:
        raise BadRequest("Unknown action.")
    return handler(services, ctx, body, record)


# Portal actions that run after the company has been resolved and checked.
COMPANY_ACTIONS: dict[str, Callable] = {
    "startOnboarding": onboarding.start_onboarding,
    "stageUpdate": onboarding.stage_update,
    "annualPlan": onboarding.annual_plan,
    "obligationCreate": lambda s, ctx, body, c: onboarding.obligation_upsert(s, ctx, body, c, existing=False),
    "obligationSave": lambda s, ctx, body, c: onboarding.obligation_upsert(s, ctx, body, c, existing=True),
    "publishTask": onboarding.publish_task,
    "threadCreate": onboarding.thread_create,
    "threadReply": lambda s, ctx, body, c: onboarding.thread_action(s, ctx, body, c, "threadReply"),
    "threadRead": lambda s, ctx, body, c: onboarding.thread_action(s, ctx, body, c, "threadRead"),
    "threadStatus": lambda s, ctx, body, c: onboarding.thread_action(s, ctx, body, c, "threadStatus"),
    "documentPublish": onboarding.document_publish,
    "invoiceCreate": billing.invoice_create,
    "invoiceIssue": billing.invoice_issue,
    "invoiceVoid": billing.invoice_void,
    "paymentReport": billing.payment_report,
    "paymentReview": billing.payment_review,
}


# The portal module a Client needs for an action; the rest are staff-only or checked in the service.
CLIENT_ACTION_MODULE: dict[str, str] = {
    "threadCreate": "inbox", "threadReply": "inbox", "threadRead": "inbox", "threadStatus": "inbox",
    "paymentReport": "invoices",
}


def portal_mutate(services: Services, ctx: Context, body: dict) -> dict:
    action = text(body.get("action"))
    authorize(ctx, [*STAFF, "Client"])
    if action == "companyCreate":
        return onboarding.company_create(services, ctx, body)
    if action in ("companySave", "companySubmit"):
        return onboarding.company_save(services, ctx, body, submit=action == "companySubmit")
    if action == "companyReview":
        return onboarding.company_review(services, ctx, body)
    referenced = ctx.find(body.get("id")) if body.get("id") is not None else None
    company_id = body.get("company") if truthy(body.get("company")) else (referenced or {}).get("company")
    c = company(ctx, company_id)
    if truthy(body.get("id")) and (referenced or {}).get("company") != c["id"]:
        raise NotFound("Record does not belong to this company.")
    handler = COMPANY_ACTIONS.get(action)
    if handler is None:
        raise BadRequest("Unknown portal action.")
    if action in CLIENT_ACTION_MODULE and not ctx.client_can(c["id"], CLIENT_ACTION_MODULE[action]):
        raise Forbidden("Your role does not permit this action.")
    return handler(services, ctx, body, c)
