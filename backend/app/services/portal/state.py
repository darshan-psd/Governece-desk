"""Client-portal read model (``GET /api/portal``), ported from ``portalState``.

Only the record kinds the portal shows are read, for the companies in scope (one indexed query),
plus batched lookups for payments, linked tasks and onboarding evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.jsvalues import gt, le, today, truthy
from app.domain.portal_domain import ACT_SOURCE, DOCUMENT_CATEGORIES, INCORPORATION_SOURCE
from app.domain.workflow_rules import CANCELLED, can_access
from app.permissions import CLIENT_HIDDEN_COMPANY_FIELDS, Context, client_safe
from app.services.portal.common import STAFF
from app.services.portal.onboarding import PUBLISHABLE_WORK
from app.services.portal.tracker import work_items

PORTAL_KINDS = ["document", "invoice", "portalobligation", "onboarding", "thread", "request", "task"]
# Above this many ids an IN (...) filter costs more than filtering the kind in Python.
MAX_IN_FILTER = 500


def _invoice_visible(ctx: Context, r: dict) -> bool:
    return ctx.role in STAFF or ctx.role == "Auditor" or r.get("status") != "Draft"


def _profile_invalid(ctx: Context, company_id: str) -> bool:
    return (ctx.find(company_id) or {}).get("profileStatus") != "Verified"


def _company_filter(ids: set[str], restricted: bool) -> dict:
    return {"where": {"company": sorted(ids)}} if restricted and len(ids) <= MAX_IN_FILTER else {}


def portal_state(ctx: Context, only_company: str | None = None) -> dict:
    """Portal state for every company the actor can access, or just ``only_company`` (exports)."""
    is_client = ctx.role == "Client"
    scope = ctx.scoped_companies
    if only_company is not None:
        company_ids = [only_company]
    else:
        company_ids = scope  # None: every company
    candidates = ctx.query(kinds=["company"], **({"ids": company_ids} if company_ids is not None else {}))
    companies = [
        {k: v for k, v in r.items() if k not in CLIENT_HIDDEN_COMPANY_FIELDS} if is_client else r
        for r in candidates if can_access(ctx.actor, r)
    ]
    ids = {c["id"] for c in companies}
    in_scope = lambda r: r.get("company") in ids  # noqa: E731
    related = [r for r in ctx.query(kinds=PORTAL_KINDS, **_company_filter(ids, company_ids is not None)) if in_scope(r)]
    by_kind: dict[str, list[dict]] = {kind: [] for kind in PORTAL_KINDS}
    for r in related:
        by_kind[r["kind"]].append(r)

    documents = [{k: v for k, v in r.items() if k != "key"} for r in by_kind["document"] if truthy(r.get("shared")) or not is_client]
    visible_invoices = [r for r in by_kind["invoice"] if _invoice_visible(ctx, r)]
    invoice_ids = {r["id"] for r in visible_invoices}
    payment_filter = {"where": {"invoice": sorted(invoice_ids)}} if len(invoice_ids) <= MAX_IN_FILTER else {}
    payments_all = [p for p in ctx.query(kinds=["payment"], **payment_filter) if p.get("invoice") in invoice_ids] if invoice_ids else []
    # One pass over the payments instead of one pass per invoice (was O(invoices x payments)).
    totals: dict[str, dict[str, int]] = {}
    for p in payments_all:
        bucket = totals.setdefault(p.get("invoice"), {"Confirmed": 0, "Pending verification": 0})
        if p.get("status") in bucket:
            bucket[p["status"]] += p.get("amount", 0)
    invoices = []
    for r in visible_invoices:
        bucket = totals.get(r["id"], {"Confirmed": 0, "Pending verification": 0})
        confirmed, pending = bucket["Confirmed"], bucket["Pending verification"]
        invoices.append({**r, "paid": confirmed, "pending": pending, "balance": (r.get("total") or 0) - confirmed})

    ctx.find_many(r.get("sourceTask") for r in by_kind["portalobligation"])  # batch-load linked tasks
    obligations = []
    # Client-safe timestamps of the internal record behind a published item, for the work tracker
    # only: they are passed to the projection and never added to the portal payload.
    mirrored: dict[str, dict] = {}
    for r in by_kind["portalobligation"]:
        source = ctx.find(r.get("sourceTask")) if r.get("sourceTask") is not None else None
        status = ("Review required" if truthy(source.get("reviewRequired")) else source.get("status")) if source else r.get("status")
        obligations.append({**r, "status": status, "due": (source or {}).get("due") or r.get("due")})
        if source is not None:
            mirrored[r["id"]] = {key: source.get(key) for key in ("created", "completedAt", "cancelledAt")}

    ctx.find_many(i for r in by_kind["onboarding"] for s in (r.get("stages") or []) for i in (s.get("evidence") or []))
    cases = []
    for r in by_kind["onboarding"]:
        stages = []
        for s in r.get("stages") or []:
            stale = s.get("status") == "Verified" and (
                _profile_invalid(ctx, r.get("company"))
                or any((lambda d: d is None or not truthy(d.get("shared")) or truthy(d.get("supersededBy")))(ctx.find(i)) for i in s.get("evidence") or [])
            )
            stages.append({**s, "status": "Review required" if stale else s.get("status")})
        cases.append({**r, "stages": stages})

    publishable = [] if is_client else [
        *by_kind["task"],
        *(r for r in ctx.query(kinds=[k for k in PUBLISHABLE_WORK if k != "task"],
                               **_company_filter(ids, company_ids is not None)) if in_scope(r)),
    ]
    actor_id = ctx.actor.get("id")
    threads = []
    for r in by_kind["thread"]:
        read_at = (r.get("readBy") or {}).get(actor_id) or ""
        unread = any(m.get("by") != actor_id and gt(m.get("at"), read_at) for m in r.get("messages") or [])
        threads.append({**r, "unread": unread})
    due = [
        *({"id": r["id"], "company": r.get("company"), "title": r.get("title"), "due": r.get("due"), "type": "Compliance"}
          for r in obligations if truthy(r.get("due")) and r.get("status") not in ("Completed", "Not applicable", CANCELLED)),
        *({"id": r["id"], "company": r.get("company"), "title": "Invoice " + str(r.get("number")), "due": r.get("due"), "type": "Payment"}
          for r in invoices if r.get("status") == "Issued" and r["balance"] > 0),
        *({"id": f"{c['id']}-{s.get('id')}", "company": c.get("company"), "title": s.get("title"), "due": s.get("due"), "type": "Onboarding"}
          for c in cases for s in c["stages"] if truthy(s.get("due")) and s.get("status") != "Verified"),
    ]
    week = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
    state = {
        "actor": ctx.actor,
        "companies": companies,
        "documents": documents,
        "cases": cases,
        "obligations": obligations,
        "invoices": invoices,
        "payments": [r for r in payments_all if in_scope(r)],
        "threads": threads,
        "requests": [client_safe(ctx, r) for r in by_kind["request"] if not is_client or r.get("assignee") == actor_id],
        "alerts": [r for r in due if le(r.get("due"), week)],
        "calendar": due,
        "documentCategories": DOCUMENT_CATEGORIES,
        "staff": ctx.role in STAFF,
        "today": today(),
        "sources": {"incorporation": INCORPORATION_SOURCE, "annual": ACT_SOURCE},
        # Staff only: internal work a secretary may publish to this client's calendar. Clients
        # receive an empty list, and nothing here is a source for the client work tracker.
        "internalTasks": [] if is_client else [
            {"id": r["id"], "kind": r["kind"], "title": r.get("title"), "company": r.get("company"),
             "due": r.get("due"), "period": r.get("period"), "category": PUBLISHABLE_WORK[r["kind"]]}
            for r in publishable if r.get("status") != CANCELLED
        ],
    }
    # Client-facing work tracker, derived from the authorised data above (no extra queries, no
    # new access path). ``internalTasks`` is not one of its sources.
    state["work"] = work_items(state, mirrored=mirrored)
    return state
