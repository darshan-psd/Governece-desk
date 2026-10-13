"""Per-request context loading and the shared optimistic-locking save."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, union_all

from app.auth import BUILTIN_OWNER, SIGN_IN_MESSAGE, Principal
from app.core.container import Services
from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized
from app.permissions import Context, WorkspaceStore, accessible
from app.models import Record, json_field
from app.repositories.identity import IdentityRepository
from app.repositories.records import ROWID, AuditRepository, RecordRepository

CHANGED = "This record changed. Refresh it before saving."


PASSWORD_CHANGE_REQUIRED = "You must change your temporary password before continuing."


def load_context(services: Services, principal: Principal, require_password_set: bool = True) -> Context:
    """Builds the request context from the session's membership (server-side state only).

    Organization, role, company scope and status come from the identity tables. The workspace
    profile record supplies display fields (name, ...) and must exist, except for the built-in
    owner profile.

    ``require_password_set``: when True (the default; every business endpoint), a member who was
    created with a temporary password and has not yet chosen their own is refused with 403. The
    handful of endpoints that let them do so (``/api/auth/me``, ``/password/change``, ``/logout``)
    pass False.
    """
    with services.db.read() as session:
        resolved = IdentityRepository(session).resolve(principal.membership_id)
    if resolved is None:
        raise Unauthorized(SIGN_IN_MESSAGE)
    if resolved.organization_status != "active":
        raise Forbidden("This organization is not active.")
    if not resolved.active:
        raise Forbidden("This workspace profile is not active.")
    if require_password_set and resolved.must_change_password:
        raise Forbidden(PASSWORD_CHANGE_REQUIRED)
    store = WorkspaceStore(services.db, resolved.organization_id)
    profile = store.get(resolved.member_id)
    if profile is not None and profile.get("kind") != "member":
        profile = None
    if profile is None:
        if resolved.member_id != BUILTIN_OWNER["id"]:
            raise Forbidden("This workspace profile is not active.")
        profile = dict(BUILTIN_OWNER)
    actor = {**profile, "role": resolved.role, "companies": resolved.companies, "active": True}
    return Context(owner=resolved.organization_id, actor=actor, store=store,
                   membership_id=resolved.membership_id, user_id=resolved.user_id,
                   must_change_password=resolved.must_change_password)


def visibility_filters(ctx: Context) -> dict:
    """SQL pre-filter for the records an actor could possibly see (company scope, client kinds).

    It only narrows the candidates: callers still apply ``accessible()``, which stays the single
    source of truth for access decisions. Expressed as ``rowid IN (… UNION ALL …)`` (``seq`` on
    PostgreSQL) so each branch uses its own index (primary key for company records, the company
    index for everything else); a plain OR would make SQLite scan the whole workspace.
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
        AuditRepository.of(session, ctx).add(audit_label(ctx, action), label)
    return {"ok": True, "id": record["id"]}
