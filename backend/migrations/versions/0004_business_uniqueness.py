"""Business uniqueness enforced by the database (works across processes and servers).

* Invoice numbers: unique per organization among issued and void invoices (drafts may share a
  number until issue, exactly as the application rule allows).
* Payment references: unique per organization and company, case-insensitively, among payments
  that are not rejected (a rejected report frees its reference, as before).
* Trial-balance files: the same file (SHA-256) is imported once per company, period and currency.

The application keeps its own checks (they give the friendly 400 messages); these indexes are the
guarantee when two requests race, possibly in different processes. Existing data is checked
first: if it already contains duplicates the migration stops (and, being transactional, changes
nothing) and lists them, rather than silently altering records.

Revision ID: 0004_business_uniqueness
Revises: 0003_authentication
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_business_uniqueness"
down_revision = "0003_authentication"
branch_labels = None
depends_on = None


def _sql(dialect: str) -> dict[str, str]:
    if dialect == "postgresql":
        f = lambda name: f"(data ->> '{name}')"  # noqa: E731
    else:
        f = lambda name: f"json_extract(data, '$.{name}')"  # noqa: E731
    invoice_where = f"kind = 'invoice' AND {f('status')} IN ('Issued', 'Void')"
    payment_where = f"kind = 'payment' AND {f('status')} <> 'Rejected'"
    import_key = f"owner, {f('company')}, {f('period')}, {f('currency')}, {f('hash')}"
    return {
        "invoice_duplicates": f"SELECT owner, {f('number')}, COUNT(*) FROM records WHERE {invoice_where} "
                              f"GROUP BY owner, {f('number')} HAVING COUNT(*) > 1",
        "payment_duplicates": f"SELECT owner, {f('company')}, lower({f('reference')}), COUNT(*) FROM records WHERE {payment_where} "
                              f"GROUP BY owner, {f('company')}, lower({f('reference')}) HAVING COUNT(*) > 1",
        "import_duplicates": f"SELECT {import_key}, COUNT(*) FROM records WHERE kind = 'trialbalance' "
                             f"GROUP BY {import_key} HAVING COUNT(*) > 1",
        "invoice_index": f"CREATE UNIQUE INDEX ux_records_invoice_number ON records (owner, {f('number')}) WHERE {invoice_where}",
        "payment_index": f"CREATE UNIQUE INDEX ux_records_payment_reference ON records (owner, {f('company')}, lower({f('reference')})) "
                         f"WHERE {payment_where}",
        "import_index": f"CREATE UNIQUE INDEX ux_records_trialbalance_file ON records ({import_key}) WHERE kind = 'trialbalance'",
    }


def upgrade() -> None:
    bind = op.get_bind()
    sql = _sql(bind.dialect.name)
    problems = [f"invoice number {row[1]!r} issued more than once in organization {row[0]!r}"
                for row in bind.execute(sa.text(sql["invoice_duplicates"]))]
    problems += [f"payment reference {row[2]!r} reported more than once for company {row[1]!r} in organization {row[0]!r}"
                 for row in bind.execute(sa.text(sql["payment_duplicates"]))]
    problems += [f"trial-balance file imported more than once for company {row[1]!r}, period {row[2]!r}, {row[3]!r} in organization {row[0]!r}"
                 for row in bind.execute(sa.text(sql["import_duplicates"]))]
    if problems:
        raise RuntimeError("Existing records break a uniqueness rule; resolve them before upgrading (nothing was changed):\n  - "
                           + "\n  - ".join(problems))
    op.execute(sql["invoice_index"])
    op.execute(sql["payment_index"])
    op.execute(sql["import_index"])


def downgrade() -> None:
    op.execute("DROP INDEX ux_records_trialbalance_file")
    op.execute("DROP INDEX ux_records_payment_reference")
    op.execute("DROP INDEX ux_records_invoice_number")
