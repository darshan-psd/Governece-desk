"""Built-in email + password authentication (``GOVERNANCE_AUTH_MODE=password``).

* Sign-in verifies an argon2id hash, checks the user and membership are active, then issues a
  server-side session (``app.auth.issue_session``). Failures share one generic message and are
  throttled per account and per client address (database-backed, so it holds across processes).
* Accounts are created by the Owner from an existing team profile: the invitation stores the
  email and mails a single-use activation link where the person chooses their own password.
  No password is ever generated, sent or stored in plain text.
* Password reset mails a single-use link (30 minutes by default); completing it ends every
  session of the account. Requesting a reset never reveals whether the email exists.
* Every outcome is recorded in ``security_events``; organization-scoped outcomes are also added to
  the organization's audit trail.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from app.auth import Principal, issue_session
from app.auth.passwords import hash_password, needs_rehash, validate_password, verify_password
from app.core.container import Services
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound, TooManyRequests, Unauthorized
from app.core.jsvalues import as_list
from app.domain.workflow_rules import SETTLED_STATUSES
from app.models import ASSIGNABLE_ROLES
from app.notifications.mail import DeliveryError, EmailMessage
from app.permissions import Context, authorize
from app.repositories.auth import AuthRepository
from app.repositories.identity import IdentityRepository
from app.repositories.records import AuditRepository, RecordRepository
from app.repositories.sessions import SessionRepository, iso_in
from app.services.context import audit_label

log = logging.getLogger("governance.auth")

INVALID_CREDENTIALS = "The email or password is incorrect."
THROTTLED = "Too many sign-in attempts. Wait a few minutes and try again."
INVALID_LINK = "This link is invalid or has expired. Request a new one."
_EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@]+\.[^\s@]{2,}$")
RESET_REQUESTS_PER_HOUR = 5


def normalise_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    return email if len(email) <= 254 and _EMAIL.match(email) else None


def _key(scope: str, value: str) -> str:
    return hashlib.sha256(f"{scope}:{value}".encode()).hexdigest()


def _require_password_mode(services: Services) -> None:
    if not services.settings.password_auth:
        raise NotFound("Not found.")


def public_url(services: Services) -> str:
    settings = services.settings
    return (settings.governance_public_url or f"http://{settings.host}:{settings.port}").rstrip("/")


def _deliver(services: Services, message: EmailMessage) -> bool:
    """Sends now; a failure is logged (never the body: it contains a single-use link)."""
    try:
        services.mailer.send(message)
        return True
    except DeliveryError as error:
        log.warning("Email delivery failed: %s", error, extra={"event": "mail_failed", "template": message.template})
        return False


# Sign-in ---------------------------------------------------------------------------------------

@dataclass
class SignInResult:
    token: str | None = None
    organizations: list[dict] = field(default_factory=list)  # set when the user must choose one


def sign_in(services: Services, email: Any, password: Any, organization_id: Any, client: str, user_agent: str | None) -> SignInResult:
    _require_password_mode(services)
    settings = services.settings
    address = normalise_email(email)
    account_key = _key("account", address or "")
    client_key = _key("client", client)
    since = iso_in(-settings.governance_login_window_minutes * 60)
    with services.db.read() as session:
        repo = AuthRepository(session)
        throttled = (repo.failures_since("account", account_key, since) >= settings.governance_login_max_failures
                     or repo.failures_since("client", client_key, since) >= settings.governance_login_max_failures_per_client)
    if throttled:
        with services.db.write() as session:
            AuthRepository(session).event("sign_in_throttled", client=client)
        raise TooManyRequests(THROTTLED)

    user = None
    if address and isinstance(password, str) and 0 < len(password) <= 1024:
        with services.db.read() as session:
            user = AuthRepository(session).user_by_email(address)
            stored = user.password_hash if user else None
        valid = verify_password(stored, password)  # runs a full hash even without an account
    else:
        verify_password(None, "x")
        valid = False
    if not valid or user is None or user.status != "active":
        with services.db.write() as session:
            repo = AuthRepository(session)
            repo.record_attempt("account", account_key, False)
            repo.record_attempt("client", client_key, False)
            repo.event("sign_in_failed", user_id=user.id if user else None, client=client,
                       reason="inactive_user" if valid and user is not None else "invalid_credentials")
        raise Unauthorized(INVALID_CREDENTIALS)

    with services.db.read() as session:
        memberships = AuthRepository(session).active_memberships(user.id)
    if not memberships:
        with services.db.write() as session:
            AuthRepository(session).event("sign_in_failed", user_id=user.id, client=client, reason="no_active_membership")
        raise Forbidden("Your account has no active workspace. Contact your workspace owner.")
    if isinstance(organization_id, str) and organization_id:
        chosen = next((m for m, o in memberships if o.id == organization_id), None)
        if chosen is None:
            raise Forbidden("You do not have access to that workspace.")
    elif len(memberships) == 1:
        chosen = memberships[0][0]
    else:
        return SignInResult(organizations=[{"id": o.id, "name": o.name} for _, o in memberships])

    token = issue_session(services, chosen.id, method="password", client=client, user_agent=user_agent)
    with services.db.write() as session:
        repo = AuthRepository(session)
        if needs_rehash(user.password_hash or ""):
            # Cosmetic rehash of the same password the user just typed; preserves any pending
            # forced-password-change state rather than clearing it.
            repo.set_password(user.id, hash_password(password), must_change_password=user.must_change_password)
        repo.clear_failures("account", account_key)
        repo.record_attempt("account", account_key, True)
        repo.mark_signed_in(user.id)
        repo.event("sign_in", organization_id=chosen.organization_id, user_id=user.id, membership_id=chosen.id, client=client)
        AuditRepository(session, chosen.organization_id, chosen.id).add(f"Signed in · {user.display_name}", "Password sign-in")
        repo.purge_attempts(iso_in(-7 * 86400))
    return SignInResult(token=token)


def sign_out(services: Services, ctx: Context, principal: Principal, everywhere: bool, client: str) -> int:
    with services.db.write() as session:
        sessions = SessionRepository(session)
        if everywhere and principal.user_id:
            count = sessions.revoke_user(principal.user_id)
        else:
            sessions.revoke(principal.token or "")
            count = 1
        AuthRepository(session).event("sign_out_everywhere" if everywhere else "sign_out", organization_id=ctx.owner,
                                      user_id=principal.user_id, membership_id=principal.membership_id, client=client, sessions=count)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Signed out everywhere" if everywhere else "Signed out"), f"{count} session(s)")
    return count


def me(services: Services, ctx: Context, principal: Principal) -> dict:
    with services.db.read() as session:
        organization = IdentityRepository(session).get_organization(ctx.owner)
        user = AuthRepository(session).get_user(ctx.user_id) if ctx.user_id else None
    return {
        "mode": services.settings.governance_auth_mode,
        "user": {"id": ctx.user_id, "name": ctx.actor.get("name"), "email": user.email if user else None},
        "organization": {"id": ctx.owner, "name": organization.name if organization else ctx.owner},
        "membership": {"id": ctx.membership_id, "member": ctx.actor.get("id"), "role": ctx.role},
        "session": {"id": principal.session_id, "method": principal.method},
        "mustChangePassword": ctx.must_change_password,
    }


def list_sessions(services: Services, principal: Principal) -> list[dict]:
    if not principal.user_id:
        return []
    with services.db.read() as session:
        rows = SessionRepository(session).active_for_user(principal.user_id)
    return [{"id": r.public_id, "created": r.created, "lastSeen": r.last_seen_at, "expires": r.expires, "client": r.client,
             "userAgent": r.user_agent, "organization": r.workspace, "current": r.public_id == principal.session_id} for r in rows]


def revoke_session(services: Services, ctx: Context, principal: Principal, session_id: Any, client: str) -> None:
    if not isinstance(session_id, str) or not principal.user_id:
        raise NotFound("Session not found.")
    with services.db.write() as session:
        if not SessionRepository(session).revoke_public(principal.user_id, session_id):
            raise NotFound("Session not found.")
        AuthRepository(session).event("session_revoked", organization_id=ctx.owner, user_id=principal.user_id,
                                      membership_id=principal.membership_id, client=client)


def change_password(services: Services, ctx: Context, principal: Principal, current: Any, new: Any, client: str) -> None:
    _require_password_mode(services)
    with services.db.read() as session:
        user = AuthRepository(session).get_user(principal.user_id or "")
    if user is None or not isinstance(current, str) or not verify_password(user.password_hash, current):
        raise BadRequest("The current password is incorrect.")
    validate_password(new, user.email, user.display_name)
    with services.db.write() as session:
        repo = AuthRepository(session)
        repo.set_password(user.id, hash_password(new), must_change_password=False)
        ended = SessionRepository(session).revoke_user(user.id, except_token=principal.token)
        repo.event("password_changed", organization_id=ctx.owner, user_id=user.id, membership_id=principal.membership_id,
                   client=client, other_sessions_ended=ended)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Changed password"), f"{ended} other session(s) ended")


# Password reset and activation -----------------------------------------------------------------

def request_password_reset(services: Services, email: Any, client: str) -> None:
    """Always succeeds from the caller's point of view (no account enumeration)."""
    _require_password_mode(services)
    address = normalise_email(email)
    if address is None:
        return
    key = _key("reset", address)
    with services.db.write() as session:
        repo = AuthRepository(session)
        if repo.failures_since("reset", key, iso_in(-3600)) >= RESET_REQUESTS_PER_HOUR:
            repo.event("password_reset_throttled", client=client)
            return
        repo.record_attempt("reset", key, False)  # counts requests, not failures
        user = repo.user_by_email(address)
        if user is None or user.status != "active" or not repo.active_memberships(user.id):
            repo.event("password_reset_requested", client=client, matched=False)
            return
        token = secrets.token_urlsafe(32)
        repo.create_token(token, "password_reset", user.id, services.settings.governance_reset_minutes)
        repo.event("password_reset_requested", user_id=user.id, client=client, matched=True)
        name, to = user.display_name, user.email
    minutes = services.settings.governance_reset_minutes
    _deliver(services, EmailMessage(
        to=to or address, subject="Reset your Governance Desk password", template="password_reset",
        text=(f"Hello {name},\n\nA password reset was requested for your Governance Desk account. Open this link within "
              f"{minutes} minutes to choose a new password:\n\n{public_url(services)}/#reset-password?token={token}\n\n"
              "If you did not ask for this, ignore this email; your password stays unchanged.")))


def _peek_user(services: Services, token: Any, purpose: str):
    from app.models import AuthToken
    from app.repositories.sessions import token_hash

    if not isinstance(token, str) or not token:
        raise BadRequest(INVALID_LINK)
    with services.db.read() as session:
        row = session.get(AuthToken, token_hash(token))
        user = AuthRepository(session).get_user(row.user_id) if row is not None else None
    if row is None or row.purpose != purpose or row.used_at is not None or row.expires_at <= iso_in(0) or user is None:
        raise BadRequest(INVALID_LINK)
    return user


def complete_password_reset(services: Services, token: Any, password: Any, client: str) -> None:
    _require_password_mode(services)
    user = _peek_user(services, token, "password_reset")
    validate_password(password, user.email, user.display_name)  # before the token is used up
    hashed = hash_password(password)
    with services.db.write() as session:
        repo = AuthRepository(session)
        row = repo.consume_token(token, "password_reset")
        if row is None or row.user_id != user.id:
            raise BadRequest(INVALID_LINK)
        repo.set_password(user.id, hashed)
        ended = SessionRepository(session).revoke_user(user.id)
        repo.clear_failures("account", _key("account", (user.email or "").lower()))
        repo.event("password_reset_completed", user_id=user.id, client=client, sessions_ended=ended)


def activate_account(services: Services, token: Any, password: Any, client: str) -> None:
    _require_password_mode(services)
    user = _peek_user(services, token, "activation")
    validate_password(password, user.email, user.display_name)
    hashed = hash_password(password)
    with services.db.write() as session:
        repo = AuthRepository(session)
        row = repo.consume_token(token, "activation")
        if row is None or row.user_id != user.id:
            raise BadRequest(INVALID_LINK)
        repo.set_password(user.id, hashed)
        organization = None
        if row.membership_id:
            resolved = IdentityRepository(session).resolve(row.membership_id)
            organization = resolved.organization_id if resolved else None
        repo.event("account_activated", organization_id=organization, user_id=user.id, membership_id=row.membership_id, client=client)
        if organization:
            AuditRepository(session, organization, row.membership_id).add(f"Activated account · {user.display_name}", user.email or "")


# Team administration (Owner) -------------------------------------------------------------------

def _membership_for(ctx: Context, session, member_id: Any):
    if not isinstance(member_id, str):
        raise NotFound("Team member not found.")
    membership = IdentityRepository(session).membership_by_member(ctx.owner, member_id)
    if membership is None:
        raise NotFound("Team member not found.")
    return membership


def invite_member(services: Services, ctx: Context, member_id: Any, email: Any, client: str) -> dict:
    """Owner: attach an email to a team profile and send its single-use activation link."""
    authorize(ctx, ["Owner"])
    _require_password_mode(services)
    address = normalise_email(email)
    if address is None:
        raise BadRequest("Enter a valid email address.")
    token = secrets.token_urlsafe(32)
    with services.db.write() as session:
        membership = _membership_for(ctx, session, member_id)
        repo = AuthRepository(session)
        user = repo.get_user(membership.user_id)
        if user is None:
            raise NotFound("Team member not found.")
        if user.password_hash:
            raise Conflict("This team member has already activated their account.")
        other = repo.user_by_email(address)
        if other is not None and other.id != user.id:
            raise Conflict("This email address already belongs to another account.")
        if membership.status != "active":
            raise BadRequest("Reactivate this team member before inviting them.")
        repo.set_email(user.id, address)
        repo.create_token(token, "activation", user.id, services.settings.governance_activation_hours * 60, membership_id=membership.id)
        repo.event("member_invited", organization_id=ctx.owner, user_id=user.id, membership_id=membership.id, client=client,
                   by=ctx.membership_id)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Invited team member"), user.display_name)
        name = user.display_name
    with services.db.read() as session:
        organization = IdentityRepository(session).get_organization(ctx.owner)
    hours = services.settings.governance_activation_hours
    delivered = _deliver(services, EmailMessage(
        to=address, subject="You have been invited to Governance Desk", template="activation",
        text=(f"Hello {name},\n\n{ctx.actor.get('name')} invited you to the {organization.name if organization else ''} workspace "
              f"on Governance Desk. Open this link within {hours} hours to choose your password and activate your account:\n\n"
              f"{public_url(services)}/#activate?token={token}\n\nIf you were not expecting this invitation, ignore this email.")))
    return {"ok": True, "delivered": delivered}


def set_member_status(services: Services, ctx: Context, member_id: Any, status: Any, client: str) -> dict:
    """Owner: deactivate (ends their sessions immediately) or reactivate a team member."""
    authorize(ctx, ["Owner"])
    if status not in ("active", "inactive"):
        raise BadRequest("Choose active or inactive.")
    if member_id == ctx.actor.get("id"):
        raise BadRequest("You cannot change your own access.")
    with services.db.write() as session:
        membership = _membership_for(ctx, session, member_id)
        identity = IdentityRepository(session)
        if membership.role == "Owner" and status == "inactive" and not _active_owners_except(session, ctx.owner, membership.id):
            raise BadRequest("The workspace must keep at least one active owner.")
        identity.set_membership_status(membership.id, status)
        records = RecordRepository(session, ctx.owner)
        profile = records.get(membership.member_id)
        if profile is not None and profile.get("kind") == "member":
            if not records.update_if_revision(profile["id"], profile["revision"], {**profile, "active": status == "active"}):
                raise Conflict("This team member changed. Reload and retry.")
        ended = SessionRepository(session).revoke_membership(membership.id) if status == "inactive" else 0
        AuthRepository(session).event("member_deactivated" if status == "inactive" else "member_reactivated",
                                      organization_id=ctx.owner, user_id=membership.user_id, membership_id=membership.id,
                                      client=client, by=ctx.membership_id, sessions_ended=ended)
        name = (profile or {}).get("name") or membership.member_id
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Deactivated team member" if status == "inactive" else "Reactivated team member"), name)
    return {"ok": True}


# Work a Client can never be the responsible person for: ``accessible()`` hides these kinds from a
# Client, so an assignment that survives a demotion is reported back to the Owner (it is not
# changed here — reassigning someone's work is the Owner's decision, not a side effect).
STAFF_ONLY_WORK = ("task", "meeting", "close", "event", "rule", "submission")


def _active_owners_except(session, organization_id: str, membership_id: str) -> list[str]:
    """Active owners of the organization other than ``membership_id``, locked in a fixed order so
    two owners changing each other at the same moment cannot both succeed (as in set_member_status)."""
    from sqlalchemy import select

    from app.models import Membership
    owners = session.scalars(select(Membership.id).where(
        Membership.organization_id == organization_id, Membership.role == "Owner", Membership.status == "active")
        .order_by(Membership.id).with_for_update()).all()
    return [owner for owner in owners if owner != membership_id]


def set_member_role(services: Services, ctx: Context, member_id: Any, role: Any, client: str) -> dict:
    """Owner: change an existing team member's role, keeping their company access exactly as it is.

    The role lives on the membership, which ``load_context`` resolves on every request, so the new
    role governs the member's next request without them signing in again (their sessions stay
    valid — this is a change of role, not a loss of access).

    Client transitions: a Client may never hold organization-wide access
    (``ck_memberships_client_scope``), so a member with ``*`` must be given specific companies
    first, through the existing company-access action. Promoting a Client to staff keeps exactly
    the companies they already had: no path here widens anyone's company access.
    """
    authorize(ctx, ["Owner"])
    if role == "Owner":
        raise BadRequest("An additional workspace owner cannot be appointed here.")
    if role not in ASSIGNABLE_ROLES:
        raise BadRequest("Choose a valid role.")
    if member_id == ctx.actor.get("id"):
        raise BadRequest("You cannot change your own role.")
    with services.db.write() as session:
        membership = _membership_for(ctx, session, member_id)
        if membership.role == role:
            raise BadRequest("This team member already has that role.")
        identity = IdentityRepository(session)
        if membership.role == "Owner" and not _active_owners_except(session, ctx.owner, membership.id):
            raise BadRequest("The workspace must keep at least one active owner.")
        records = RecordRepository(session, ctx.owner)
        profile = records.get(membership.member_id)
        profile = profile if profile is not None and profile.get("kind") == "member" else None
        warning = None
        if role == "Client":
            scoped = [c for c in as_list((profile or {}).get("companies")) if c != "*"]
            if membership.all_companies or "*" in as_list((profile or {}).get("companies")) or not scoped:
                raise BadRequest("Assign specific companies to this team member before making them a client.")
            # Internal work still pointing at them: reported, never reassigned automatically.
            held = [r for r in records.find(kinds=STAFF_ONLY_WORK, where={"assignee": membership.member_id})
                    if r.get("status") not in SETTLED_STATUSES]
            if held:
                warning = (f"{len(held)} internal record(s) still name this person as the responsible person; "
                           "a client cannot see them. Reassign that work.")
        previous = membership.role
        identity.set_membership_role(membership.id, role)
        if profile is not None:
            if not records.update_if_revision(profile["id"], profile["revision"], {**profile, "role": role}):
                raise Conflict("This team member changed. Reload and retry.")
        AuthRepository(session).event("member_role_changed", organization_id=ctx.owner, user_id=membership.user_id,
                                      membership_id=membership.id, client=client, by=ctx.membership_id,
                                      previous_role=previous, role=role)
        name = (profile or {}).get("name") or membership.member_id
        AuditRepository.of(session, ctx).add(audit_label(ctx, f"Changed role to {role}"), name)
    return {"ok": True, "role": role, **({"warning": warning} if warning else {})}


def set_member_companies(services: Services, ctx: Context, member_id: Any, companies: Any, client: str) -> dict:
    """Owner: replace a team member's company access (add/remove), preserving their role.

    Never grants ``"*"`` through this path — only specific companies the Owner selects — so a
    member can never end up with implicit access to every company via this action.
    """
    authorize(ctx, ["Owner"])
    if member_id == ctx.actor.get("id"):
        raise BadRequest("You cannot change your own access.")
    requested = as_list(companies)
    with services.db.write() as session:
        membership = _membership_for(ctx, session, member_id)
        if membership.role == "Owner":
            raise BadRequest("The Owner's company access cannot be changed here.")
        records = RecordRepository(session, ctx.owner)
        known = records.get_many(c for c in requested if isinstance(c, str))
        selected = [c for c in requested if isinstance(c, str) and c in known and known[c].get("kind") == "company"]
        if not selected:
            raise BadRequest("Assign at least one company.")
        IdentityRepository(session).set_company_scope(ctx.owner, membership.id, selected)
        profile = records.get(membership.member_id)
        if profile is not None and profile.get("kind") == "member":
            if not records.update_if_revision(profile["id"], profile["revision"], {**profile, "companies": selected}):
                raise Conflict("This team member changed. Reload and retry.")
        AuthRepository(session).event("member_companies_changed", organization_id=ctx.owner, user_id=membership.user_id,
                                      membership_id=membership.id, client=client, by=ctx.membership_id, companies=selected)
        name = (profile or {}).get("name") or membership.member_id
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Updated company access"), name)
    return {"ok": True, "companies": selected}


def team_accounts(services: Services, ctx: Context) -> list[dict]:
    """Owner: sign-in status of every team profile (email, activated, membership status)."""
    authorize(ctx, ["Owner"])
    from sqlalchemy import select

    from app.models import Membership, User
    with services.db.read() as session:
        rows = session.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                               .where(Membership.organization_id == ctx.owner).order_by(Membership.created_at)).all()
    return [{"member": m.member_id, "role": m.role, "status": m.status, "email": u.email, "activated": u.password_hash is not None,
             "lastSignIn": u.last_sign_in_at} for m, u in rows]


def security_events(services: Services, ctx: Context, limit: int, offset: int) -> dict:
    authorize(ctx, ["Owner"])
    with services.db.read() as session:
        items, total = AuthRepository(session).events_for_organization(ctx.owner, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
