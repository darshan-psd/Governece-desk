"""Baseline: the existing records, audit and sessions tables and their indexes.

Works on two kinds of database:
* a new, empty database: creates everything;
* an existing database (created by the Node backend or by the FastAPI app before Alembic):
  adopts it. Existing tables are kept exactly as they are (their columns are checked first);
  only missing tables and indexes are created. No row is read, rewritten or deleted.

Revision ID: 0001_baseline
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

EXPECTED_COLUMNS = {
    "records": {"owner", "id", "kind", "data", "revision", "updated"},
    "audit": {"id", "owner", "action", "subject", "at"},
    "sessions": {"token_hash", "workspace", "actor", "created", "expires"},
}
# Exactly the definitions already in use (Node migration + the FastAPI scaling phase), so an
# existing index is recognised by name and expression indexes still match the queries.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS records_owner_kind ON records (owner, kind)",
    "CREATE INDEX IF NOT EXISTS audit_owner_at ON audit (owner, at)",
    "CREATE INDEX IF NOT EXISTS ix_records_owner_company_kind ON records (owner, json_extract(data, '$.company'), kind)",
    "CREATE INDEX IF NOT EXISTS ix_records_owner_kind_updated ON records (owner, kind, updated)",
]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    for table, expected in EXPECTED_COLUMNS.items():
        if table in existing:
            found = {column["name"] for column in inspector.get_columns(table)}
            missing = expected - found
            if missing:
                raise RuntimeError(f"Existing table '{table}' is missing column(s) {sorted(missing)}; refusing to adopt this database.")
    if "records" not in existing:
        op.create_table(
            "records",
            sa.Column("owner", sa.Text(), nullable=False),
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("data", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("updated", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("owner", "id"),
        )
    if "audit" not in existing:
        op.create_table(
            "audit",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("owner", sa.Text(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("at", sa.Text(), nullable=False),
        )
    if "sessions" not in existing:
        op.create_table(
            "sessions",
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column("workspace", sa.Text(), nullable=False),
            sa.Column("actor", sa.Text(), nullable=False),
            sa.Column("created", sa.Text(), nullable=False),
            sa.Column("expires", sa.Text(), nullable=False),
        )
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("The baseline cannot be downgraded: it would drop every table and all workspace data.")
