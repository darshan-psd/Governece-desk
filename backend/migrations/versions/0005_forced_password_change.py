"""Forced password change: a flag set when the Owner creates a member with a temporary password.

Additive only: one nullable-with-default column on ``users``. Works the same on SQLite and
PostgreSQL (``server_default`` is portable).

Revision ID: 0005_forced_password_change
Revises: 0004_business_uniqueness
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_forced_password_change"
down_revision = "0004_business_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    raise RuntimeError(
        "0005_forced_password_change cannot be downgraded automatically: it would drop the "
        "must_change_password flag. Restore the pre-upgrade backup instead.")
