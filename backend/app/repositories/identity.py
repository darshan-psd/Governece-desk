"""Data access for organizations, users, memberships, company access and background leases."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.core.jsvalues import now_iso
from app.database.dialect import insert_for
from app.models import ROLES, BackgroundLease, CompanyAccess, Membership, Organization, User


@dataclass(frozen=True)
class ResolvedMembership:
    """Everything the server needs to trust about who is acting, read in one query."""

    organization_id: str
    organization_status: str
    user_id: str
    user_status: str
    membership_id: str
    member_id: str
    role: str
    status: str
    all_companies: bool
    company_ids: tuple[str, ...]
    must_change_password: bool = False

    @property
    def active(self) -> bool:
        return self.organization_status == "active" and self.user_status == "active" and self.status == "active"

    @property
    def companies(self) -> list[str]:
        """Company scope in the shape the rules already use: ``["*"]`` or explicit company ids."""
        return ["*"] if self.all_companies else list(self.company_ids)


class IdentityRepository:
    def __init__(self, session: Session):
        self.session = session

    # Organizations ------------------------------------------------------------------------------
    def get_organization(self, organization_id: str) -> Organization | None:
        return self.session.get(Organization, organization_id)

    def create_organization(self, organization_id: str, name: str) -> Organization:
        now = now_iso()
        organization = Organization(id=organization_id, name=name, status="active", created_at=now, updated_at=now)
        self.session.add(organization)
        self._flush("An organization with this id already exists.")
        return organization

    def set_organization_status(self, organization_id: str, status: str) -> None:
        self.session.execute(update(Organization).where(Organization.id == organization_id).values(status=status, updated_at=now_iso()))

    def active_organization_ids(self) -> list[str]:
        return list(self.session.scalars(select(Organization.id).where(Organization.status == "active").order_by(Organization.id)))

    # Users and memberships ---------------------------------------------------------------------
    def create_user(self, display_name: str, email: str | None = None, user_id: str | None = None) -> User:
        now = now_iso()
        user = User(id=user_id or str(uuid.uuid4()), email=email, display_name=display_name, status="active", created_at=now, updated_at=now)
        self.session.add(user)
        self._flush("A user with this email already exists.")
        return user

    def set_user_status(self, user_id: str, status: str) -> None:
        self.session.execute(update(User).where(User.id == user_id).values(status=status, updated_at=now_iso()))

    def create_membership(self, organization_id: str, user_id: str, member_id: str, role: str, all_companies: bool,
                          membership_id: str | None = None) -> Membership:
        if role not in ROLES:
            raise BadRequest("Unknown role.")
        now = now_iso()
        membership = Membership(id=membership_id or str(uuid.uuid4()), organization_id=organization_id, user_id=user_id,
                                member_id=member_id, role=role, status="active", all_companies=1 if all_companies else 0,
                                created_at=now, updated_at=now)
        self.session.add(membership)
        self._flush("This user already has a membership in the organization.", conflict=True)
        return membership

    def membership_by_member(self, organization_id: str, member_id: str) -> Membership | None:
        return self.session.scalars(select(Membership).where(
            Membership.organization_id == organization_id, Membership.member_id == member_id)).first()

    def set_membership_status(self, membership_id: str, status: str) -> None:
        self.session.execute(update(Membership).where(Membership.id == membership_id).values(status=status, updated_at=now_iso()))

    def grant_company(self, organization_id: str, membership_id: str, company_id: str) -> bool:
        """Idempotent. The database refuses companies of another organization or non-company records."""
        statement = insert_for(self.session, CompanyAccess).values(
            organization_id=organization_id, membership_id=membership_id, company_id=company_id, created_at=now_iso(),
        ).on_conflict_do_nothing().returning(CompanyAccess.company_id)  # rowcount is unreliable for INSERT
        return self.session.execute(statement).first() is not None

    def set_company_scope(self, organization_id: str, membership_id: str, company_ids: list[str]) -> None:
        """Replaces a membership's company access with exactly ``company_ids``: revokes whatever
        isn't listed, grants whatever is missing, and clears ``all_companies`` (this path never
        grants ``"*"`` — only the specific companies the Owner selected)."""
        self.session.execute(update(Membership).where(Membership.id == membership_id, Membership.organization_id == organization_id)
                             .values(all_companies=0, updated_at=now_iso()))
        self.session.execute(delete(CompanyAccess).where(
            CompanyAccess.organization_id == organization_id, CompanyAccess.membership_id == membership_id,
            CompanyAccess.company_id.notin_(company_ids)))
        for company_id in company_ids:
            self.grant_company(organization_id, membership_id, company_id)

    def resolve(self, membership_id: str) -> ResolvedMembership | None:
        row = self.session.execute(text(
            "SELECT o.id, o.status, u.id, u.status, m.id, m.member_id, m.role, m.status, m.all_companies, u.must_change_password "
            "FROM organization_memberships m JOIN organizations o ON o.id = m.organization_id JOIN users u ON u.id = m.user_id "
            "WHERE m.id = :id"), {"id": membership_id}).first()
        if row is None:
            return None
        companies = tuple(self.session.scalars(
            select(CompanyAccess.company_id).where(CompanyAccess.membership_id == membership_id, CompanyAccess.organization_id == row[0])
            .order_by(CompanyAccess.created_at, CompanyAccess.company_id)))
        return ResolvedMembership(organization_id=row[0], organization_status=row[1], user_id=row[2], user_status=row[3],
                                  membership_id=row[4], member_id=row[5], role=row[6], status=row[7],
                                  all_companies=bool(row[8]), company_ids=companies, must_change_password=bool(row[9]))

    # Background leases -------------------------------------------------------------------------
    def acquire_lease(self, name: str, holder: str, seconds: int) -> bool:
        """Atomically takes (or renews) a lease; True only for the single current holder."""
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        current = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        statement = insert_for(self.session, BackgroundLease).values(name=name, holder=holder, expires_at=expires)
        statement = statement.on_conflict_do_update(
            index_elements=["name"], set_={"holder": holder, "expires_at": expires},
            where=(BackgroundLease.expires_at < current) | (BackgroundLease.holder == holder),
        ).returning(BackgroundLease.holder)  # a row comes back only when this holder inserted or took it over
        return self.session.execute(statement).first() is not None

    def _flush(self, message: str, conflict: bool = False) -> None:
        try:
            self.session.flush()
        except IntegrityError as error:
            raise (Conflict if conflict else BadRequest)(message) from error
