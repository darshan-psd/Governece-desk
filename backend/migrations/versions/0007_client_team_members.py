"""Client team members: username sign-in and company-level client roles.

Additive only: three new nullable columns, one partial unique index and one check constraint.
No existing row is rewritten. ``client_role`` NULL means a company's primary client contact, so
every existing client keeps exactly the access they have today.

Revision ID: 0007_client_team_members
Revises: 0006_company_identifiers
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_client_team_members"
down_revision = "0006_company_identifiers"
branch_labels = None
depends_on = None

CLIENT_ROLE_CHECK = "client_role IS NULL OR client_role IN ('Company Admin', 'Director', 'Accountant', 'Employee')"


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.Text(), nullable=True))
    op.execute("CREATE UNIQUE INDEX ux_users_username ON users (lower(username)) WHERE username IS NOT NULL")
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot add a constraint to an existing table, but accepts one on a new column.
        op.execute(f"ALTER TABLE company_access ADD COLUMN client_role TEXT CONSTRAINT ck_company_access_client_role CHECK ({CLIENT_ROLE_CHECK})")
    else:
        op.add_column("company_access", sa.Column("client_role", sa.Text(), nullable=True))
        op.create_check_constraint("ck_company_access_client_role", "company_access", CLIENT_ROLE_CHECK)
    op.add_column("company_access", sa.Column("client_modules", sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError(
        "0007_client_team_members cannot be downgraded automatically: it would drop usernames and "
        "client roles, so team members could no longer sign in. Restore the pre-upgrade backup instead.")
