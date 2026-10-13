"""Authentication: user credentials, session lifecycle, activation/reset tokens, rate limiting, security events.

Additive only: new nullable columns and new tables. Existing rows are not rewritten; existing
sessions keep working until they expire (they have no idle timeout). In password mode only
sessions created by a password sign-in are accepted, so earlier preview sessions grant nothing.

Revision ID: 0003_authentication
Revises: 0002_identity_tenancy
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_authentication"
down_revision = "0002_identity_tenancy"
branch_labels = None
depends_on = None

USER_COLUMNS = ("password_hash", "password_changed_at", "last_sign_in_at")
SESSION_COLUMNS = ("public_id", "user_id", "method", "last_seen_at", "idle_expires", "revoked_at", "client", "user_agent")


def upgrade() -> None:
    for name in USER_COLUMNS:
        op.add_column("users", sa.Column(name, sa.Text(), nullable=True))
    for name in SESSION_COLUMNS:
        op.add_column("sessions", sa.Column(name, sa.Text(), nullable=True))
    op.create_index("ux_sessions_public_id", "sessions", ["public_id"], unique=True)
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    op.create_index("ix_sessions_membership", "sessions", ["membership_id"])

    op.create_table(
        "auth_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("membership_id", sa.Text(), sa.ForeignKey("organization_memberships.id"), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("used_at", sa.Text(), nullable=True),
        sa.CheckConstraint("purpose IN ('activation', 'password_reset')", name="ck_auth_tokens_purpose"),
    )
    op.create_index("ix_auth_tokens_user", "auth_tokens", ["user_id", "purpose"])

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_login_attempts_key", "login_attempts", ["scope", "key", "at"])

    op.create_table(
        "security_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("membership_id", sa.Text(), nullable=True),
        sa.Column("client", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_security_events_org_at", "security_events", ["organization_id", "at"])
    op.create_index("ix_security_events_at", "security_events", ["at"])


def downgrade() -> None:
    raise RuntimeError(
        "0003_authentication cannot be downgraded automatically: it would drop password hashes and security "
        "events. Restore the pre-upgrade backup instead.")
