"""Organizations, users and memberships: creation and the development preview bootstrap.

Workspace profiles (``records`` of kind ``member``) stay the display profiles the React app and
business records use. Memberships and company-access rows are the authoritative identity and
authorization source; every code path that creates or changes a profile writes both in the same
transaction.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.container import Services
from app.models import Membership, Organization
from app.repositories.identity import IdentityRepository
from app.repositories.records import RecordRepository

log = logging.getLogger("governance.identity")
OWNER_MEMBER_ID = "owner"
# Same namespace as migration 0002, so adopted and newly bootstrapped identities use one scheme.
_NAMESPACE = uuid.UUID("6f1d5f3e-4b7a-4c4e-9a52-2b8c7d1e9f10")


def identity_ids(organization_id: str, member_id: str) -> tuple[str, str]:
    return (str(uuid.uuid5(_NAMESPACE, f"user:{organization_id}:{member_id}")),
            str(uuid.uuid5(_NAMESPACE, f"membership:{organization_id}:{member_id}")))


def create_organization(session: Session, organization_id: str, name: str, owner_name: str = "Workspace owner",
                        owner_profile: bool = True) -> tuple[Organization, Membership]:
    """A new firm with its Owner membership (``owner`` profile, all companies).

    ``owner_profile`` also creates the owner's workspace profile record, so the owner can be
    assigned work immediately. The development preview organization skips it: without profiles
    the React app seeds its demo team on first launch (existing behaviour).
    """
    repo = IdentityRepository(session)
    organization = repo.create_organization(organization_id, name)
    user_id, membership_id = identity_ids(organization_id, OWNER_MEMBER_ID)
    repo.create_user(owner_name, user_id=user_id)
    membership = repo.create_membership(organization_id, user_id, OWNER_MEMBER_ID, "Owner", True, membership_id=membership_id)
    if owner_profile:
        RecordRepository(session, organization_id).insert(OWNER_MEMBER_ID, "member", {
            "id": OWNER_MEMBER_ID, "name": owner_name, "role": "Owner", "companies": ["*"], "active": True})
    return organization, membership


def add_member(session: Session, organization_id: str, member_id: str, display_name: str, role: str, companies: list) -> Membership:
    """User + membership + company access for a workspace profile (same transaction as the profile)."""
    repo = IdentityRepository(session)
    user_id, membership_id = identity_ids(organization_id, member_id)
    repo.create_user(display_name, user_id=user_id)
    membership = repo.create_membership(organization_id, user_id, member_id, role, "*" in companies, membership_id=membership_id)
    for company_id in companies:
        if company_id != "*":
            repo.grant_company(organization_id, membership.id, company_id)
    return membership


def prepare_new_credentials(services: Services, email, password, confirm_password, display_name: str):
    """Validates a new sign-in (password mode only) before anything is written to the database.

    Returns ``(None, None)`` outside password mode, where the caller should skip credentials
    entirely. Shared by every code path that lets the Owner (or other staff, for a Client) set a
    member's password at profile-creation time, so the policy is enforced identically everywhere.
    """
    if not services.settings.password_auth:
        return None, None
    from app.auth.passwords import validate_password
    from app.core.errors import BadRequest
    from app.services.auth_service import normalise_email

    address = normalise_email(email)
    if address is None:
        raise BadRequest("Enter a valid email address.")
    if password != confirm_password:
        raise BadRequest("The passwords do not match.")
    validate_password(password, address, display_name)  # length/common-password/name-or-email checks
    return address, password


def apply_new_credentials(session: Session, services: Services, ctx, membership: Membership, email: str, password: str) -> None:
    """Sets a new membership's sign-in email and password, in the caller's write transaction.

    The password set here is the member's real password from the first sign-in onward — no forced
    change is required. The member can still change it any time from their own account settings.
    """
    from app.auth.passwords import hash_password
    from app.repositories.auth import AuthRepository

    auth_repo = AuthRepository(session)
    auth_repo.set_email(membership.user_id, email)  # raises Conflict if already taken
    auth_repo.set_password(membership.user_id, hash_password(password), must_change_password=False)
    auth_repo.event("member_created_with_password", organization_id=ctx.owner, user_id=membership.user_id,
                    membership_id=membership.id, by=ctx.membership_id)


def set_credentials(session: Session, membership_id: str, email: str, password: str) -> None:
    """Operator bootstrap (CLI): gives a membership's user an email and a password they typed.

    The password is validated against the policy and stored only as an argon2id hash.
    """
    from app.auth.passwords import hash_password, validate_password
    from app.core.errors import BadRequest, Conflict
    from app.repositories.auth import AuthRepository
    from app.services.auth_service import normalise_email

    address = normalise_email(email)
    if address is None:
        raise BadRequest("Enter a valid email address.")
    resolved = IdentityRepository(session).resolve(membership_id)
    if resolved is None:
        raise BadRequest("Unknown membership.")
    repo = AuthRepository(session)
    user = repo.get_user(resolved.user_id)
    other = repo.user_by_email(address)
    if other is not None and other.id != resolved.user_id:
        raise Conflict("This email address already belongs to another account.")
    validate_password(password, address, user.display_name if user else None)
    repo.set_email(resolved.user_id, address)
    repo.set_password(resolved.user_id, hash_password(password))
    repo.event("credentials_set_by_operator", organization_id=resolved.organization_id, user_id=resolved.user_id,
               membership_id=membership_id)


def ensure_preview_organization(services: Services) -> None:
    """Development/test only: make sure the preview organization and its owner exist."""
    settings = services.settings
    if not settings.local_preview or settings.is_deployed:
        return
    from app.core.errors import ApiError

    try:
        with services.db.write() as session:
            if IdentityRepository(session).get_organization(settings.governance_workspace_id) is None:
                create_organization(session, settings.governance_workspace_id, "Local preview workspace", owner_profile=False)
                log.info("Created the local preview organization", extra={"event": "preview_organization_created"})
    except ApiError:
        # Several worker processes starting together: another one created it first (its transaction
        # won the unique key). Anything else is a real failure and is raised.
        with services.db.read() as session:
            if IdentityRepository(session).get_organization(settings.governance_workspace_id) is None:
                raise
