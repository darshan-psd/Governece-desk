"""Per-request context loading and the shared optimistic-locking save."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, union_all

from app.auth import BUILTIN_OWNER, Principal
from app.core.container import Services
from app.core.errors import Conflict, Forbidden, NotFound
from app.permissions import Context, WorkspaceStore, accessible
from app.models import Record, json_field
from app.repositories.records import ROWID, AuditRepository, RecordRepository

CHANGED = "This record changed. Refresh it before saving."


def load_context(services: Services, principal: Principal) -> Context:
    """Resolves the acting profile with one primary-key lookup; records are queried on demand."""
    store = WorkspaceStore(services.db, principal.workspace)
    actor = store.get(principal.actor_id)
    if actor is not None and actor.get("kind") != "member":
        actor = None
    if actor is None and principal.actor_id == "owner":
        actor = dict(BUILTIN_OWNER)
    if actor is None or actor.get("active") is False:
        raise Forbidden("This workspace profile is not active.")
    return Context(owner=principal.workspace, actor=actor, store=store)


def visibility_filters(ctx: Context) -> dict:
    """SQL pre-filter for the records an actor could possibly see (company scope, client kinds).

    It only narrows the candidates: callers still apply ``accessible()``, which stays the single
    source of truth for access decisions. Expressed as ``rowid IN (… UNION ALL …)`` so each branch
    uses its own index (primary key for company records, the company index for everything else);
    a plain OR would make SQLite scan the whole workspace.
    """
    scope = ctx.scoped_companies
    if scope is None:
        return {}
    own_companies = select(ROWID).where(Record.owner == ctx.owner, Record.kind == "company", Record.id.in_(scope))
    in_companies = select(ROWID).where(Record.owner == ctx.owner, json_field("company").in_(scope))
    if ctx.role == "Client":
        in_companies = in_companies.where(Record.kind.in_(["request", "document"]))
    return {"extra": [ROWID.in_(union_all(own_companies, in_companies))], "rowid_driven": True}


def audit_label(ctx: Context, action: str) -> str:
    return f"{action} · {ctx.actor.get('name')}"


def same_revision(body: dict, record: dict) -> bool:
    revision = body.get("revision")
    return isinstance(revision, int) and not isinstance(revision, bool) and revision == record.get("revision")


def target(ctx: Context, body: dict) -> dict:
    """The record an action applies to: must be visible to the actor and at the sent revision."""
    record = ctx.find(body.get("id"))
    if record is None or not accessible(ctx, record):
        raise NotFound("Record not found.")
    if not same_revision(body, record):
        raise Conflict(CHANGED)
    return record


def save(services: Services, ctx: Context, record: dict, data: dict, action: str,
         subject: Any = None, conflict: str = CHANGED) -> dict:
    """Writes ``data`` only if ``record`` is unchanged since it was read; audits in the same transaction."""
    with services.db.write() as session:
        if not RecordRepository(session, ctx.owner).update_if_revision(record["id"], record["revision"], data):
            raise Conflict(conflict)
        label = subject if subject is not None else (record.get("title") or record.get("name") or "")
        AuditRepository(session, ctx.owner).add(audit_label(ctx, action), label)
    return {"ok": True, "id": record["id"]}
