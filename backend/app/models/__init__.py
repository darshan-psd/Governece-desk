"""SQLAlchemy models.

``records`` and ``audit`` map the existing tables created by the Node backend (see
``drizzle/0000_redundant_luke_cage.sql``) so existing data keeps working unchanged.
``sessions`` is new: it holds server-side login sessions so the auth layer can later be
replaced by real authentication without touching the services.
"""
from __future__ import annotations

from sqlalchemy import ColumnElement, Index, Integer, PrimaryKeyConstraint, String, Text, func, literal_column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    data: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated: Mapped[str] = mapped_column(Text, nullable=False)


def json_field(name: str) -> ColumnElement:
    """``json_extract(data, '$.<name>')`` with a literal path, so SQLite can match expression indexes."""
    return func.json_extract(Record.data, literal_column(f"'$.{name}'"))


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


class AuthSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created: Mapped[str] = mapped_column(Text, nullable=False)
    expires: Mapped[str] = mapped_column(Text, nullable=False)
