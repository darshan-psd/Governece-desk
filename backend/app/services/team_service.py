"""Team Members: a client company's people, managed from the client portal.

A Company Admin creates accounts directly (username and password, no email anywhere) and manages
them. The firm's Owner and Secretaries can do the same for a company they have access to, which is
also how a company whose only admin is locked out gets back in.

Every operation starts from the server's own view of the caller. A company id sent by the browser
is only accepted when the caller may manage that company, and a member id is only looked up inside
that company of this organization, so anything else reads as "not found".

Two rules protect everyone else:

* Account-level changes (name, username, password, deactivation) reach every company the person
  belongs to, so they need authority over all of those companies. Their role here, and removing
  them from here, stay within this company and always need only this company.
* A company always keeps an active Company Admin. The check runs after locking the company's access
  rows, in a separate statement, so two admins changing each other at the same moment cannot both
  succeed and leave nobody in charge.

A team member is always a ``Client`` to the firm. This module never changes a membership's
organization role and never touches staff memberships: the lookups only match ``Client`` rows.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.auth.passwords import hash_password, validate_password
from app.core.container import Services
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.jsvalues import as_list, now_iso, text
from app.domain.client_access import CLIENT_ROLES, COMPANY_ADMIN, EMPLOYEE, EMPLOYEE_MODULES, company_grant
from app.domain.workflow_rules import can_access
from app.models import AuthSession, CompanyAccess, Membership, User
from app.permissions import Context
from app.repositories.auth import AuthRepository
from app.repositories.identity import IdentityRepository
from app.repositories.records import AuditRepository, RecordRepository
from app.repositories.sessions import SessionRepository
from app.services.auth_service import clear_sign_in_failures, normalise_username
from app.services.context import audit_label
from app.services.identity_service import add_member
from app.services.portal.common import company
from app.services.portal.state import TEAM_MANAGERS

NAME_MAX = 200
DESIGNATION_MAX = 120
USERNAME_RULE = "Use 3 to 32 letters, numbers, dots, hyphens or underscores for the username, starting and ending with a letter or number."
USERNAME_TAKEN = "This username is already taken."
SHARED_ACCOUNT = "This person also belongs to another company, so their account can only be changed by your company secretary."
CHANGED = "This team member changed. Reload and retry."


@dataclass
class Target:
    """The person an action applies to, as stored."""

    membership: Membership
    user: User
    access: CompanyAccess  # their row for this company
    companies: list[str]   # every company they belong to
    profile: dict | None

    @property
    def name(self) -> str:
        return (self.profile or {}).get("name") or self.user.display_name


# Who may act ------------------------------------------------------------------------------------

def _may_manage(ctx: Context, company_id: str) -> bool:
    return ctx.client_can(company_id, "team") if ctx.role == "Client" else ctx.role in TEAM_MANAGERS


def _managed_company(ctx: Context, company_id: Any) -> dict:
    c = company(ctx, company_id)  # 404 for any company outside the caller's own scope
    if not _may_manage(ctx, c["id"]):
        raise Forbidden("Only a Company Admin can manage team members.")
    return c


def _manages_all(ctx: Context, companies: list[str]) -> bool:
    return all(_may_manage(ctx, c) and can_access(ctx.actor, {"kind": "company", "id": c}) for c in companies)


def _account_scope(ctx: Context, target: Target) -> None:
    if not _manages_all(ctx, target.companies):
        raise Forbidden(SHARED_ACCOUNT)


def _is_self(ctx: Context, target: Target) -> bool:
    return target.membership.member_id == ctx.actor.get("id")


def _target(session, ctx: Context, c: dict, member_id: Any) -> Target:
    """The person, only if they are a client user of this company in this organization."""
    if not isinstance(member_id, str) or not member_id:
        raise NotFound("Team member not found.")
    row = session.execute(_members(ctx.owner, c["id"]).where(Membership.member_id == member_id)).first()
    if row is None:
        raise NotFound("Team member not found.")
    membership, user, access = row
    companies = list(session.scalars(select(CompanyAccess.company_id).where(
        CompanyAccess.organization_id == ctx.owner, CompanyAccess.membership_id == membership.id)))
    profile = RecordRepository(session, ctx.owner).get(membership.member_id)
    return Target(membership, user, access, companies, profile if (profile or {}).get("kind") == "member" else None)


def _members(organization_id: str, company_id: str):
    return (select(Membership, User, CompanyAccess)
            .join(User, User.id == Membership.user_id)
            .join(CompanyAccess, and_(CompanyAccess.membership_id == Membership.id, CompanyAccess.organization_id == Membership.organization_id))
            .where(Membership.organization_id == organization_id, Membership.role == "Client", CompanyAccess.company_id == company_id))


def _active_admins(session, organization_id: str, company_id: str) -> set[str]:
    return set(session.scalars(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .join(CompanyAccess, and_(CompanyAccess.membership_id == Membership.id, CompanyAccess.organization_id == Membership.organization_id))
        .where(Membership.organization_id == organization_id, Membership.role == "Client", Membership.status == "active",
               User.status == "active", CompanyAccess.company_id == company_id,
               or_(CompanyAccess.client_role.is_(None), CompanyAccess.client_role == COMPANY_ADMIN))))


def _keep_an_admin(session, organization_id: str, membership_id: str, companies: list[str]) -> None:
    """Refuses when ``membership_id`` is the last active Company Admin of one of ``companies``.

    The rows are locked first, in a fixed order, and counted by a second statement: under READ
    COMMITTED a waiting statement keeps its old snapshot, so counting in the locking statement itself
    could still see an admin who was deactivated while it waited.
    """
    session.scalars(select(CompanyAccess.membership_id).where(
        CompanyAccess.organization_id == organization_id, CompanyAccess.company_id.in_(companies))
        .order_by(CompanyAccess.company_id, CompanyAccess.membership_id).with_for_update()).all()
    for company_id in companies:
        if _active_admins(session, organization_id, company_id) == {membership_id}:
            raise BadRequest("Every company must keep at least one active Company Admin. Make someone else an admin first.")


# Input ------------------------------------------------------------------------------------------

def _username(value: Any) -> str:
    username = normalise_username(value)
    if username is None:
        raise BadRequest(USERNAME_RULE)
    return username


def _role_and_modules(role: Any, modules: Any) -> tuple[str, list[str] | None]:
    """A client-side role only: the firm's roles (Secretary, Owner, ...) are refused here."""
    if role not in CLIENT_ROLES:
        raise BadRequest("Choose a valid role.")
    if role != EMPLOYEE:
        return role, None
    chosen = as_list(modules)
    if any(m not in EMPLOYEE_MODULES for m in chosen):
        raise BadRequest("Choose valid modules for this employee.")
    return role, [m for m in EMPLOYEE_MODULES if m in chosen]


def _new_password(password: Any, confirm: Any, username: str | None, name: str) -> str:
    if password != confirm:
        raise BadRequest("The passwords do not match.")
    return validate_password(password, username, name)


def _save_profile(records: RecordRepository, target: Target, changes: dict) -> None:
    profile = target.profile
    if profile is not None and not records.update_if_revision(profile["id"], profile["revision"], {**profile, **changes}):
        raise Conflict(CHANGED)


def _record(session, ctx: Context, client: str, event: str, action: str, target_name: str, c: dict,
            user_id: str, membership_id: str, **detail: Any) -> None:
    """Security event and audit entry, in the caller's transaction. Never contains a password."""
    AuthRepository(session).event(event, organization_id=ctx.owner, user_id=user_id, membership_id=membership_id,
                                  client=client, by=ctx.membership_id, company=c["id"], **detail)
    AuditRepository.of(session, ctx).add(audit_label(ctx, action), f"{target_name} · {c.get('name')}")


# Reading ----------------------------------------------------------------------------------------

def team_list(services: Services, ctx: Context, company_id: Any) -> dict:
    """The company's team. Sign-in secrets are never selected into the response."""
    c = _managed_company(ctx, company_id)
    with services.db.read() as session:
        rows = session.execute(_members(ctx.owner, c["id"])).all()
        ids = [m.id for m, _user, _access in rows]
        companies: dict[str, list[str]] = {i: [] for i in ids}
        seen: dict[str, str] = {}
        if ids:
            for membership_id, other in session.execute(select(CompanyAccess.membership_id, CompanyAccess.company_id).where(
                    CompanyAccess.organization_id == ctx.owner, CompanyAccess.membership_id.in_(ids))):
                companies[membership_id].append(other)
            seen = dict(session.execute(select(AuthSession.membership_id, func.max(AuthSession.last_seen_at)).where(
                AuthSession.membership_id.in_(ids)).group_by(AuthSession.membership_id)).all())
        admins = _active_admins(session, ctx.owner, c["id"])
    profiles = ctx.find_many((m.member_id for m, _user, _access in rows), "member")
    members = []
    for membership, user, access in rows:
        grant = company_grant(access.client_role, access.client_modules)
        profile = profiles.get(membership.member_id) or {}
        active = membership.status == "active" and user.status == "active"
        members.append({
            "member": membership.member_id, "name": profile.get("name") or user.display_name, "username": user.username,
            "email": user.email, "role": grant.role, "modules": list(grant.modules), "designation": profile.get("designation") or "",
            "company": {"id": c["id"], "name": c.get("name")}, "status": "Active" if active else "Deactivated",
            "lastActive": max(filter(None, (seen.get(membership.id), user.last_sign_in_at)), default=None),
            "self": membership.member_id == ctx.actor.get("id"), "accountManaged": _manages_all(ctx, companies[membership.id]),
            "lastAdmin": admins == {membership.id}, "otherCompanies": len(companies[membership.id]) - 1,
        })
    members.sort(key=lambda m: (m["name"].casefold(), m["member"]))
    active_count = sum(1 for m in members if m["status"] == "Active")
    return {"company": {"id": c["id"], "name": c.get("name")}, "members": members,
            "summary": {"total": len(members), "active": active_count, "deactivated": len(members) - active_count}}


# Changing ---------------------------------------------------------------------------------------

def team_create(services: Services, ctx: Context, body: dict, client: str) -> dict:
    """Creates a working account directly: no email, no invitation, active from the first sign-in."""
    c = _managed_company(ctx, body.get("company"))
    name = text(body.get("name"), NAME_MAX)
    if not name:
        raise BadRequest("Enter the team member's full name.")
    username = _username(body.get("username"))
    role, modules = _role_and_modules(body.get("role"), body.get("modules"))
    password = _new_password(body.get("password"), body.get("confirmPassword"), username, name)
    with services.db.read() as session:
        if AuthRepository(session).user_by_username(username) is not None:
            raise Conflict(USERNAME_TAKEN)
    hashed = hash_password(password)  # slow on purpose, so outside the write transaction
    member_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(member_id, "member", {
            "name": name, "role": "Client", "companies": [c["id"]], "active": True, "preview": services.settings.local_preview,
            "designation": text(body.get("designation"), DESIGNATION_MAX), "createdBy": ctx.actor["id"], "createdAt": now_iso(),
        })
        membership = add_member(session, ctx.owner, member_id, name, "Client", [c["id"]])
        IdentityRepository(session).set_client_role(ctx.owner, membership.id, c["id"], role, modules)
        repo = AuthRepository(session)
        repo.set_username(membership.user_id, username)  # the unique index settles a race with another request
        repo.set_password(membership.user_id, hashed, must_change_password=False)
        _record(session, ctx, client, "team_member_created", f"Created team member ({role})", name, c,
                membership.user_id, membership.id, role=role)
    return {"ok": True, "member": member_id}


def team_update(services: Services, ctx: Context, body: dict, client: str) -> dict:
    """Edits details (account level) and the role in this company, in one transaction."""
    c = _managed_company(ctx, body.get("company"))
    name = text(body["name"], NAME_MAX) if "name" in body else None
    if name == "":
        raise BadRequest("Enter the team member's full name.")
    designation = text(body["designation"], DESIGNATION_MAX) if "designation" in body else None
    username = _username(body["username"]) if body.get("username") else None
    role, modules = _role_and_modules(body.get("role"), body.get("modules")) if "role" in body else (None, None)
    with services.db.write() as session:
        target = _target(session, ctx, c, body.get("member"))
        repo, user, own = AuthRepository(session), target.user, _is_self(ctx, target)
        details: dict = {}
        if name is not None and name != target.name:
            details["name"] = name
        if designation is not None and designation != ((target.profile or {}).get("designation") or ""):
            details["designation"] = designation
        new_username = username if username and username != user.username else None
        if details or new_username:
            if not own:
                _account_scope(ctx, target)
            if new_username:
                other = repo.user_by_username(new_username)
                if other is not None and other.id != user.id:
                    raise Conflict(USERNAME_TAKEN)
                repo.set_username(user.id, new_username)
            if "name" in details:
                repo.set_display_name(user.id, details["name"])
            _save_profile(RecordRepository(session, ctx.owner), target, details)
            _record(session, ctx, client, "team_member_updated", "Updated team member details", details.get("name") or target.name, c,
                    user.id, target.membership.id, fields=sorted([*details, *(["username"] if new_username else [])]))
        grant = company_grant(target.access.client_role, target.access.client_modules)
        if role is not None and (role != grant.role or tuple(modules or ()) != grant.modules):
            if own:
                raise BadRequest("You cannot change your own role.")
            if grant.role == COMPANY_ADMIN and role != COMPANY_ADMIN:
                _keep_an_admin(session, ctx.owner, target.membership.id, [c["id"]])
            IdentityRepository(session).set_client_role(ctx.owner, target.membership.id, c["id"], role, modules)
            _record(session, ctx, client, "team_member_role_changed", f"Changed team member role to {role}", target.name, c,
                    user.id, target.membership.id, previous_role=grant.role, role=role, modules=modules)
    return {"ok": True}


def team_password(services: Services, ctx: Context, body: dict, client: str) -> dict:
    """Sets a new password for a member and ends their sessions. No email is involved."""
    c = _managed_company(ctx, body.get("company"))
    with services.db.read() as session:
        target = _target(session, ctx, c, body.get("member"))
    if _is_self(ctx, target):
        raise BadRequest("Change your own password from your account, not from the team list.")
    _account_scope(ctx, target)
    if not target.user.username and not target.user.email:
        raise BadRequest("Give this team member a username first, so they can sign in.")
    password = _new_password(body.get("password"), body.get("confirmPassword"), target.user.username or target.user.email, target.name)
    hashed = hash_password(password)
    with services.db.write() as session:
        target = _target(session, ctx, c, body.get("member"))
        repo = AuthRepository(session)
        repo.set_password(target.user.id, hashed, must_change_password=False)
        ended = SessionRepository(session).revoke_user(target.user.id)
        clear_sign_in_failures(repo, target.user)
        _record(session, ctx, client, "team_member_password_reset", "Reset team member password", target.name, c,
                target.user.id, target.membership.id, sessions_ended=ended)
    return {"ok": True}


def team_status(services: Services, ctx: Context, body: dict, client: str) -> dict:
    """Deactivates (ending their sessions at once) or reactivates a member's account."""
    c = _managed_company(ctx, body.get("company"))
    status = body.get("status")
    if status not in ("active", "inactive"):
        raise BadRequest("Choose active or inactive.")
    with services.db.write() as session:
        target = _target(session, ctx, c, body.get("member"))
        if _is_self(ctx, target):
            raise BadRequest("You cannot change your own access.")
        _account_scope(ctx, target)
        membership = target.membership
        if membership.status == status:
            raise BadRequest("This team member already has that status.")
        if status == "inactive":
            _keep_an_admin(session, ctx.owner, membership.id, target.companies)
        IdentityRepository(session).set_membership_status(membership.id, status)
        _save_profile(RecordRepository(session, ctx.owner), target, {"active": status == "active"})
        ended = SessionRepository(session).revoke_membership(membership.id) if status == "inactive" else 0
        _record(session, ctx, client, "team_member_deactivated" if status == "inactive" else "team_member_reactivated",
                "Deactivated team member" if status == "inactive" else "Reactivated team member", target.name, c,
                target.user.id, membership.id, sessions_ended=ended)
    return {"ok": True}


def team_remove(services: Services, ctx: Context, body: dict, client: str) -> dict:
    """Removes a member from this company.

    Someone who also belongs to another company simply loses this one. Someone with no company
    left loses their account: they can never sign in again and their username is freed. Their
    profile stays, so past work keeps showing who did it.
    """
    c = _managed_company(ctx, body.get("company"))
    with services.db.write() as session:
        target = _target(session, ctx, c, body.get("member"))
        if _is_self(ctx, target):
            raise BadRequest("You cannot remove yourself.")
        membership = target.membership
        _keep_an_admin(session, ctx.owner, membership.id, [c["id"]])
        remaining = [x for x in target.companies if x != c["id"]]
        identity, records = IdentityRepository(session), RecordRepository(session, ctx.owner)
        identity.revoke_company(ctx.owner, membership.id, c["id"])
        closed = not remaining
        ended = 0
        if closed:
            identity.set_membership_status(membership.id, "inactive")
            ended = SessionRepository(session).revoke_membership(membership.id)
            AuthRepository(session).close_account(target.user.id)
        _save_profile(records, target, {"companies": remaining, **({"active": False, "removedAt": now_iso(), "removedBy": ctx.actor["id"]} if closed else {})})
        waiting = [r for r in records.find(kinds=["request"], where={"company": c["id"], "assignee": membership.member_id})
                   if r.get("status") != "Closed"]
        _record(session, ctx, client, "team_member_removed", "Removed team member", target.name, c,
                target.user.id, membership.id, account_closed=closed, sessions_ended=ended)
    warning = f"{len(waiting)} open request(s) were addressed to this person. Ask your company secretary to reassign them." if waiting else None
    return {"ok": True, **({"warning": warning} if warning else {})}
