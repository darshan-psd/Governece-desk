"""Identity and tenancy: organizations, users, memberships, company access, background leases.

Adoption of existing data (nothing is deleted, rewritten or renumbered):
* every distinct tenant key already used (``records.owner``, ``audit.owner``, ``sessions.workspace``)
  becomes an organization with that same id, so record ids, keys and file paths stay valid;
* every workspace profile (``records`` of kind ``member``) becomes a user plus a membership with
  the same role, status and company scope; the profile record itself is left untouched;
* each organization gets an Owner membership for the built-in ``owner`` profile if it has none;
* existing sessions are linked to the membership of the profile they were issued for.

Company references in a profile that do not exist as company records of the same organization
cannot be represented (the foreign key would fail). They grant no access today, so they are
skipped and reported in the migration log; the profile record still contains them.

The migration aborts (and, being transactional, leaves the database unchanged) when a profile
has an unknown role or a Client profile has access to every company.

Revision ID: 0002_identity_tenancy
Revises: 0001_baseline
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0002_identity_tenancy"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")
ROLES = ("Owner", "Secretary", "Accountant", "Director", "Auditor", "Client")
# Fixed namespace: re-running the adoption produces the same ids (idempotent inserts).
NAMESPACE = uuid.UUID("6f1d5f3e-4b7a-4c4e-9a52-2b8c7d1e9f10")
PREVIEW_ORGANIZATION = "local-preview"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def identity_ids(organization_id: str, member_id: str) -> tuple[str, str]:
    """Deterministic (user id, membership id) for a workspace profile."""
    return (str(uuid.uuid5(NAMESPACE, f"user:{organization_id}:{member_id}")),
            str(uuid.uuid5(NAMESPACE, f"membership:{organization_id}:{member_id}")))


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def create_schema() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_in("status", ("active", "inactive")), name="ck_organizations_status"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(_in("status", ("active", "inactive")), name="ck_users_status"),
    )
    op.execute("CREATE UNIQUE INDEX ux_users_email ON users (lower(email)) WHERE email IS NOT NULL")
    op.create_table(
        "organization_memberships",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("organization_id", sa.Text(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("member_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("all_companies", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_memberships_org_user"),
        sa.UniqueConstraint("organization_id", "member_id", name="uq_memberships_org_member"),
        sa.UniqueConstraint("organization_id", "id", name="uq_memberships_org_id"),
        sa.CheckConstraint(_in("role", ROLES), name="ck_memberships_role"),
        sa.CheckConstraint(_in("status", ("active", "inactive")), name="ck_memberships_status"),
        sa.CheckConstraint("all_companies IN (0, 1)", name="ck_memberships_all_companies"),
        sa.CheckConstraint("role != 'Client' OR all_companies = 0", name="ck_memberships_client_scope"),
    )
    op.create_index("ix_memberships_user", "organization_memberships", ["user_id"])
    op.create_table(
        "company_access",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("membership_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("membership_id", "company_id"),
        sa.ForeignKeyConstraint(["organization_id", "membership_id"], ["organization_memberships.organization_id", "organization_memberships.id"],
                                name="fk_company_access_membership", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "company_id"], ["records.owner", "records.id"], name="fk_company_access_company"),
    )
    op.create_index("ix_company_access_company", "company_access", ["organization_id", "company_id"])
    op.create_table(
        "background_leases",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("holder", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
    )
    # Added in place (no table rebuild): existing rows keep their position and content.
    op.execute("ALTER TABLE sessions ADD COLUMN membership_id TEXT REFERENCES organization_memberships (id)")
    op.execute("ALTER TABLE audit ADD COLUMN actor_membership_id TEXT")


TRIGGERS = {
    # Tenant integrity for the existing tables without rebuilding them: a record or audit entry must
    # belong to a known organization, and can never be moved to another one.
    "trg_records_owner_known": """CREATE TRIGGER trg_records_owner_known BEFORE INSERT ON records
        WHEN NOT EXISTS (SELECT 1 FROM organizations WHERE id = NEW.owner)
        BEGIN SELECT RAISE(ABORT, 'records.owner must be an existing organization'); END""",
    "trg_records_owner_immutable": """CREATE TRIGGER trg_records_owner_immutable BEFORE UPDATE OF owner ON records
        WHEN NEW.owner IS NOT OLD.owner
        BEGIN SELECT RAISE(ABORT, 'records cannot move between organizations'); END""",
    "trg_audit_owner_known": """CREATE TRIGGER trg_audit_owner_known BEFORE INSERT ON audit
        WHEN NOT EXISTS (SELECT 1 FROM organizations WHERE id = NEW.owner)
        BEGIN SELECT RAISE(ABORT, 'audit.owner must be an existing organization'); END""",
    "trg_audit_owner_immutable": """CREATE TRIGGER trg_audit_owner_immutable BEFORE UPDATE OF owner ON audit
        WHEN NEW.owner IS NOT OLD.owner
        BEGIN SELECT RAISE(ABORT, 'audit entries cannot move between organizations'); END""",
    # Company access may only reference a company record (the foreign key guarantees same tenant).
    "trg_company_access_is_company": """CREATE TRIGGER trg_company_access_is_company BEFORE INSERT ON company_access
        WHEN NOT EXISTS (SELECT 1 FROM records WHERE owner = NEW.organization_id AND id = NEW.company_id AND kind = 'company')
        BEGIN SELECT RAISE(ABORT, 'company_access must reference a company of the same organization'); END""",
    # A session's compatibility columns must agree with its membership.
    "trg_sessions_membership_consistent": """CREATE TRIGGER trg_sessions_membership_consistent BEFORE INSERT ON sessions
        WHEN NEW.membership_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM organization_memberships m
            WHERE m.id = NEW.membership_id AND m.organization_id = NEW.workspace AND m.member_id = NEW.actor)
        BEGIN SELECT RAISE(ABORT, 'session does not match its membership'); END""",
}


POSTGRES_GUARDS = [
    # PostgreSQL can add real foreign keys to existing tables, so tenant existence is a constraint.
    "ALTER TABLE records ADD CONSTRAINT fk_records_owner FOREIGN KEY (owner) REFERENCES organizations (id)",
    "ALTER TABLE audit ADD CONSTRAINT fk_audit_owner FOREIGN KEY (owner) REFERENCES organizations (id)",
    """CREATE FUNCTION gd_forbid_owner_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.owner IS DISTINCT FROM OLD.owner THEN
            RAISE EXCEPTION '% rows cannot move between organizations', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER trg_records_owner_immutable BEFORE UPDATE OF owner ON records FOR EACH ROW EXECUTE FUNCTION gd_forbid_owner_change()",
    "CREATE TRIGGER trg_audit_owner_immutable BEFORE UPDATE OF owner ON audit FOR EACH ROW EXECUTE FUNCTION gd_forbid_owner_change()",
    """CREATE FUNCTION gd_company_access_is_company() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM records WHERE owner = NEW.organization_id AND id = NEW.company_id AND kind = 'company') THEN
            RAISE EXCEPTION 'company_access must reference a company of the same organization' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER trg_company_access_is_company BEFORE INSERT ON company_access FOR EACH ROW EXECUTE FUNCTION gd_company_access_is_company()",
    """CREATE FUNCTION gd_session_matches_membership() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.membership_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM organization_memberships m
                WHERE m.id = NEW.membership_id AND m.organization_id = NEW.workspace AND m.member_id = NEW.actor) THEN
            RAISE EXCEPTION 'session does not match its membership' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER trg_sessions_membership_consistent BEFORE INSERT ON sessions FOR EACH ROW EXECUTE FUNCTION gd_session_matches_membership()",
]


def _ignore(conn, statement: str) -> sa.TextClause:
    """``INSERT OR IGNORE`` on SQLite, ``INSERT ... ON CONFLICT DO NOTHING`` on PostgreSQL."""
    if conn.dialect.name == "postgresql":
        statement = statement.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
    return sa.text(statement)


def adopt_existing_data(conn) -> dict:
    """Creates organizations/users/memberships for existing data. Safe to run more than once."""
    now = _now()
    order = "seq" if conn.dialect.name == "postgresql" else "rowid"
    report = {"organizations": 0, "memberships": 0, "company_access": 0, "sessions_linked": 0, "skipped_company_access": []}
    # PostgreSQL: records/audit keys use the "C" collation; make the UNION's collation explicit.
    collate = ' COLLATE "C"' if conn.dialect.name == "postgresql" else ""
    owners = [row[0] for row in conn.execute(sa.text(
        f"SELECT owner{collate} FROM records UNION SELECT owner{collate} FROM audit UNION SELECT workspace{collate} FROM sessions"))]
    for owner in owners:
        name = "Local preview workspace" if owner == PREVIEW_ORGANIZATION else f"Workspace {owner}"
        report["organizations"] += conn.execute(_ignore(conn,
            "INSERT OR IGNORE INTO organizations (id, name, status, created_at, updated_at) VALUES (:id, :name, 'active', :now, :now)"),
            {"id": owner, "name": name, "now": now}).rowcount
    profiles = conn.execute(sa.text(f"SELECT owner, id, CAST(data AS TEXT) FROM records WHERE kind = 'member' ORDER BY owner, {order}")).all()
    seen_owner_profile = set()
    for owner, member_id, raw in profiles:
        data = json.loads(raw) if raw else {}
        data = data if isinstance(data, dict) else {}
        role = data.get("role")
        if role not in ROLES:
            raise RuntimeError(f"Profile {member_id!r} in organization {owner!r} has an unknown role {role!r}; migration stopped.")
        companies = data.get("companies") if isinstance(data.get("companies"), list) else []
        all_companies = 1 if "*" in companies else 0
        if role == "Client" and all_companies:
            raise RuntimeError(f"Client profile {member_id!r} in organization {owner!r} has access to every company; migration stopped.")
        status = "inactive" if data.get("active") is False else "active"
        user_id, membership_id = identity_ids(owner, member_id)
        conn.execute(_ignore(conn,
            "INSERT OR IGNORE INTO users (id, email, display_name, status, created_at, updated_at) VALUES (:id, NULL, :name, :status, :now, :now)"),
            {"id": user_id, "name": str(data.get("name") or member_id), "status": status, "now": now})
        report["memberships"] += conn.execute(_ignore(conn,
            "INSERT OR IGNORE INTO organization_memberships (id, organization_id, user_id, member_id, role, status, all_companies, created_at, updated_at) "
            "VALUES (:id, :org, :user, :member, :role, :status, :all, :now, :now)"),
            {"id": membership_id, "org": owner, "user": user_id, "member": member_id, "role": role, "status": status, "all": all_companies, "now": now}).rowcount
        if member_id == "owner":
            seen_owner_profile.add(owner)
        for company_id in companies:
            if company_id == "*" or not isinstance(company_id, str):
                continue
            exists = conn.execute(sa.text("SELECT 1 FROM records WHERE owner = :o AND id = :c AND kind = 'company'"), {"o": owner, "c": company_id}).first()
            if not exists:
                report["skipped_company_access"].append(f"{owner}/{member_id} -> {company_id}")
                continue
            report["company_access"] += conn.execute(_ignore(conn,
                "INSERT OR IGNORE INTO company_access (organization_id, membership_id, company_id, created_at) VALUES (:o, :m, :c, :now)"),
                {"o": owner, "m": membership_id, "c": company_id, "now": now}).rowcount
    for owner in owners:
        if owner in seen_owner_profile:
            continue
        # The built-in owner profile (used before any profile record exists).
        user_id, membership_id = identity_ids(owner, "owner")
        conn.execute(_ignore(conn,
            "INSERT OR IGNORE INTO users (id, email, display_name, status, created_at, updated_at) VALUES (:id, NULL, 'Workspace owner', 'active', :now, :now)"),
            {"id": user_id, "now": now})
        report["memberships"] += conn.execute(_ignore(conn,
            "INSERT OR IGNORE INTO organization_memberships (id, organization_id, user_id, member_id, role, status, all_companies, created_at, updated_at) "
            "VALUES (:id, :org, :user, 'owner', 'Owner', 'active', 1, :now, :now)"),
            {"id": membership_id, "org": owner, "user": user_id, "now": now}).rowcount
    report["sessions_linked"] = conn.execute(sa.text(
        "UPDATE sessions SET membership_id = (SELECT m.id FROM organization_memberships m "
        "WHERE m.organization_id = sessions.workspace AND m.member_id = sessions.actor) WHERE membership_id IS NULL "
        "AND EXISTS (SELECT 1 FROM organization_memberships m WHERE m.organization_id = sessions.workspace AND m.member_id = sessions.actor)")).rowcount
    return report


def upgrade() -> None:
    create_schema()
    report = adopt_existing_data(op.get_bind())
    for statement in (POSTGRES_GUARDS if op.get_bind().dialect.name == "postgresql" else TRIGGERS.values()):
        op.execute(statement)
    log.info("Identity adoption: %s organization(s), %s membership(s), %s company access row(s), %s session(s) linked",
             report["organizations"], report["memberships"], report["company_access"], report["sessions_linked"])
    for skipped in report["skipped_company_access"]:
        log.warning("Company access not migrated (company does not exist; it granted no access): %s", skipped)


def downgrade() -> None:
    raise RuntimeError(
        "0002_identity_tenancy cannot be downgraded automatically: it would drop organizations, users and memberships "
        "created after the upgrade. Restore the pre-upgrade backup instead.")
