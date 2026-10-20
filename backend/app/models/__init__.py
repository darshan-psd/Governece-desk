"""SQLAlchemy models.

``records`` and ``audit`` map the existing tables created by the Node backend (see
``drizzle/0000_redundant_luke_cage.sql``) so existing data keeps working unchanged.
``sessions`` holds server-side login sessions. Phase 2 adds the identity and tenancy tables
(organizations, users, memberships, company access) described below.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean, CheckConstraint, ColumnElement, ForeignKey, ForeignKeyConstraint, Index, Integer,
    PrimaryKeyConstraint, String, Text, UniqueConstraint, func, literal_column,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.database.dialect import JsonDocument, json_text


class Base(DeclarativeBase):
    pass


class Record(Base):
    """Generic workspace record. ``kind`` selects the entity type; fields live in ``data`` (JSON).

    Kinds: company, member, task, meeting, close, event, rule, request, submission, document,
    trialbalance, pack, briefing, notification, onboarding, portalobligation, thread, invoice,
    payment. Normalising these into dedicated tables is planned for the PostgreSQL phase.
    """

    __tablename__ = "records"
    __table_args__ = (
        PrimaryKeyConstraint("owner", "id"),
        Index("records_owner_kind", "owner", "kind"),
    )

    owner: Mapped[str] = mapped_column(Text, nullable=False)
    id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON text in Python; TEXT on SQLite, JSONB on PostgreSQL (see app.database.dialect).
    data: Mapped[str] = mapped_column(JsonDocument, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated: Mapped[str] = mapped_column(Text, nullable=False)


def json_field(name: str) -> ColumnElement:
    """A top-level field of a record's data, with a literal path so expression indexes match
    (``json_extract(data, '$.<name>')`` on SQLite, ``(data ->> '<name>')`` on PostgreSQL)."""
    return json_text(Record.data, name)


# Indexes only; the stored data and the original two tables are unchanged.
# Most targeted queries select records of a company, optionally of some kinds (portal state, a
# client's workspace, payments of an invoice, new-version and event checks, company scope).
Index("ix_records_owner_company_kind", Record.owner, json_field("company"), Record.kind)
# Paginated lists of one kind, newest first (GET /api/v2/records?kind=...): avoids sorting the
# whole kind to return one page.
Index("ix_records_owner_kind_updated", Record.owner, Record.kind, Record.updated)


class AuditEvent(Base):
    __tablename__ = "audit"
    __table_args__ = (Index("audit_owner_at", "owner", "at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    # Membership that performed the action, when known from a server-side session (new events only;
    # historical entries keep NULL rather than an invented actor).
    actor_membership_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ux_sessions_public_id", "public_id", unique=True),
        Index("ix_sessions_user", "user_id"),
        Index("ix_sessions_membership", "membership_id"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace: Mapped[str] = mapped_column(Text, nullable=False)  # organization id (kept for compatibility)
    actor: Mapped[str] = mapped_column(Text, nullable=False)      # workspace profile id (kept for compatibility)
    created: Mapped[str] = mapped_column(Text, nullable=False)
    expires: Mapped[str] = mapped_column(Text, nullable=False)
    # The membership this session acts as. Sessions without one cannot be used (fail closed).
    membership_id: Mapped[str | None] = mapped_column(Text, ForeignKey("organization_memberships.id"), nullable=True)
    # Phase 3: lifecycle. ``idle_expires`` slides forward on use (NULL = no idle timeout, preview
    # sessions); ``revoked_at`` ends a session immediately (sign-out, deactivation, password change).
    public_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str | None] = mapped_column(Text, nullable=True)  # "password" | "preview"
    last_seen_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    idle_expires: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    client: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


# Identity and tenancy (Phase 2) ---------------------------------------------------------------
#
# An organization is one independent firm. Its id is the tenant key stored in ``records.owner`` and
# ``audit.owner``, so existing data belongs to the organization whose id equals its ``owner``.

ROLES = ("Owner", "Secretary", "Accountant", "Director", "Auditor", "Client")
# Roles an Owner can give a team member, when creating one or changing an existing one. ``Owner``
# is not among them: an organization's Owner comes from creating the organization, and the
# application has never had a path that appoints another one.
ASSIGNABLE_ROLES = ("Secretary", "Accountant", "Director", "Client", "Auditor")
STATUSES = ("active", "inactive")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (CheckConstraint(_in("status", STATUSES), name="ck_organizations_status"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    """A person. ``password_hash`` (argon2id) is NULL until the account is activated."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in("status", STATUSES), name="ck_users_status"),
        # Emails are optional until invitations exist, and unique regardless of case when present.
        Index("ux_users_email", func.lower(literal_column("email")), unique=True, sqlite_where=literal_column("email IS NOT NULL"),
              postgresql_where=literal_column("email IS NOT NULL")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_changed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sign_in_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when an Owner creates the account with a temporary password (Team & access); cleared the
    # first time the member changes their password. Enforced server-side in app.services.context.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Membership(Base):
    """A user's membership of one organization, with its organization-level role.

    ``member_id`` is the user's workspace profile id inside the organization (the id business
    records already use for preparer, assignee, approvals...). ``all_companies`` grants every
    company of the organization; otherwise access is limited to ``company_access`` rows.
    """

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_memberships_org_user"),
        UniqueConstraint("organization_id", "member_id", name="uq_memberships_org_member"),
        UniqueConstraint("organization_id", "id", name="uq_memberships_org_id"),
        CheckConstraint(_in("role", ROLES), name="ck_memberships_role"),
        CheckConstraint(_in("status", STATUSES), name="ck_memberships_status"),
        CheckConstraint("all_companies IN (0, 1)", name="ck_memberships_all_companies"),
        CheckConstraint("role != 'Client' OR all_companies = 0", name="ck_memberships_client_scope"),
        Index("ix_memberships_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[str] = mapped_column(Text, ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("users.id"), nullable=False)
    member_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    all_companies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class CompanyAccess(Base):
    """One company a membership may access. Both keys share ``organization_id``, so a membership can
    never be granted a company of another organization (enforced by the foreign keys)."""

    __tablename__ = "company_access"
    __table_args__ = (
        PrimaryKeyConstraint("membership_id", "company_id"),
        ForeignKeyConstraint(["organization_id", "membership_id"], ["organization_memberships.organization_id", "organization_memberships.id"],
                             name="fk_company_access_membership", ondelete="CASCADE"),
        ForeignKeyConstraint(["organization_id", "company_id"], ["records.owner", "records.id"], name="fk_company_access_company"),
        Index("ix_company_access_company", "organization_id", "company_id"),
    )

    organization_id: Mapped[str] = mapped_column(Text, nullable=False)
    membership_id: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class BackgroundLease(Base):
    """Cross-process lease so only one application process runs a background job at a time."""

    __tablename__ = "background_leases"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    holder: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)


# Authentication (Phase 3) ---------------------------------------------------------------------

TOKEN_PURPOSES = ("activation", "password_reset")


class AuthToken(Base):
    """Single-use account activation / password-reset token. Only its SHA-256 hash is stored."""

    __tablename__ = "auth_tokens"
    __table_args__ = (
        CheckConstraint(_in("purpose", TOKEN_PURPOSES), name="ck_auth_tokens_purpose"),
        Index("ix_auth_tokens_user", "user_id", "purpose"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("users.id"), nullable=False)
    membership_id: Mapped[str | None] = mapped_column(Text, ForeignKey("organization_memberships.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class LoginAttempt(Base):
    """Sign-in attempts for rate limiting. ``key`` is a SHA-256 hash (of the email or client address)."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_key", "scope", "key", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)  # "account" | "client" | "reset"
    key: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    success: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SecurityEvent(Base):
    """Authentication and account events (sign-in, failures, resets, deactivation...).

    Kept apart from the organization audit trail because some events (a failed sign-in for an
    unknown email) belong to no organization. ``detail`` never contains passwords or tokens.
    """

    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_org_at", "organization_id", "at"), Index("ix_security_events_at", "at"))

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    organization_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    membership_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    client: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
