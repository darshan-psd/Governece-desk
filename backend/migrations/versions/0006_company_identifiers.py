"""Company statutory identifiers are unique per organization, enforced by the database.

A CIN (``registration``), PAN or GSTIN names one legal entity, so two companies of the same
organization cannot carry the same one. ``check_company_duplicates()`` already rejects this with a
friendly 400 on both creation paths (internal Companies module and Client Portal), but it reads the
existing companies before the write transaction: two concurrent requests can both pass that check.
These partial unique indexes are the guarantee when requests race, in one process or several.

Comparison is normalised exactly as the application normalises it: whitespace removed and
upper-cased (the stored document still keeps what the user typed). Blank and absent identifiers are
excluded — a proposed company legitimately has none, and those must not collide with each other.

Company *names* are deliberately not unique: two real, unrelated companies can share a name, so
the application only warns about an exact name match.

Existing data is checked first: if it already contains duplicate identifiers the migration stops
(and, being transactional, changes nothing) and lists them, rather than altering records.

Revision ID: 0006_company_identifiers
Revises: 0005_forced_password_change
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_company_identifiers"
down_revision = "0005_forced_password_change"
branch_labels = None
depends_on = None

# JSON field → the label used in the application's own message for that field.
IDENTIFIERS = (("registration", "CIN / registration number"), ("pan", "PAN"), ("gstin", "GSTIN"))


def _expressions(dialect: str, name: str) -> tuple[str, str]:
    """``(raw, normalised)`` SQL for a company's ``name`` identifier field."""
    if dialect == "postgresql":
        raw = f"(data ->> '{name}')"
        normalised = f"upper(regexp_replace({raw}, '[[:space:]]', '', 'g'))"
    else:
        raw = f"json_extract(data, '$.{name}')"
        # SQLite has no regexp; strip the whitespace characters a typed identifier can contain.
        stripped = raw
        for character in ("' '", "char(9)", "char(10)", "char(13)"):
            stripped = f"replace({stripped}, {character}, '')"
        normalised = f"upper({stripped})"
    return raw, normalised


def _sql(dialect: str, name: str) -> dict[str, str]:
    raw, normalised = _expressions(dialect, name)
    where = f"kind = 'company' AND {raw} IS NOT NULL AND {normalised} <> ''"
    return {
        "duplicates": f"SELECT owner, {normalised}, COUNT(*) FROM records WHERE {where} "
                      f"GROUP BY owner, {normalised} HAVING COUNT(*) > 1",
        "index": f"CREATE UNIQUE INDEX ux_records_company_{name} ON records (owner, {normalised}) WHERE {where}",
    }


def upgrade() -> None:
    bind = op.get_bind()
    statements = [(label, _sql(bind.dialect.name, name)) for name, label in IDENTIFIERS]
    problems = [f"{label} {row[1]!r} used by more than one company in organization {row[0]!r}"
                for label, sql in statements for row in bind.execute(sa.text(sql["duplicates"]))]
    if problems:
        raise RuntimeError("Existing companies share a statutory identifier; resolve them before upgrading "
                           "(nothing was changed):\n  - " + "\n  - ".join(problems))
    for _label, sql in statements:
        op.execute(sql["index"])


def downgrade() -> None:
    for name, _label in reversed(IDENTIFIERS):
        op.execute(f"DROP INDEX ux_records_company_{name}")
