# PostgreSQL

Governance Desk runs on **PostgreSQL** in staging and production (required: the application refuses
to start there without it) and on **SQLite** for local development and isolated tests. The same
code, migrations and business rules run on both. SQLAlchemy and Alembic are unchanged; the few
SQL differences live in `backend/app/database/dialect.py`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GOVERNANCE_DATABASE_URL` | empty (SQLite file in the data directory) | `postgresql+psycopg://user:password@host:5432/dbname`. Treated as a secret: masked in reprs, never logged. `postgres://` / `postgresql://` are accepted. |
| `GOVERNANCE_DB_POOL_SIZE` | 5 | Connections kept open per application process |
| `GOVERNANCE_DB_MAX_OVERFLOW` | 5 | Extra connections under load per process |
| `GOVERNANCE_DB_POOL_TIMEOUT` | 10 | Seconds to wait for a free connection |
| `GOVERNANCE_DB_POOL_RECYCLE` | 1800 | Seconds before a connection is replaced |
| `GOVERNANCE_DB_STATEMENT_TIMEOUT_MS` | 30000 | Server-side limit per statement |
| `GOVERNANCE_DB_LOCK_TIMEOUT_MS` | 10000 | Server-side limit when waiting for a row lock |

Size the pool so that `processes × (POOL_SIZE + MAX_OVERFLOW)` stays well below the server's
`max_connections` (leave room for migrations, backups and administration). Behind PgBouncer use
session pooling, or transaction pooling with the pool size reduced accordingly.

The database role used by the application needs `CONNECT`, `USAGE` on the schema and
`SELECT/INSERT/UPDATE/DELETE` on the tables. Migrations need DDL rights; run them with a separate
migration role if your policy requires it.

## Schema decisions

* `records.data` is **JSONB** with a `CHECK (jsonb_typeof(data) = 'object')`. The application still
  reads and writes JSON text, so the services did not change.
* `records.seq` / `audit.seq` are identity columns holding the insertion order (SQLite's `rowid`);
  every list order uses them as the tie-breaker, so ordering is identical on both databases.
* The key text columns of `records` and `audit` use the bytewise `"C"` collation, matching SQLite's
  ordering and comparison exactly. Sorting on a JSON field also uses `"C"` and SQLite's NULL order.
* **Real foreign keys** from `records.owner` and `audit.owner` to `organizations` (SQLite uses triggers
  for the same rule). Triggers keep a record's organization immutable and check company access and
  session consistency; violations surface as integrity errors (SQLSTATE 23000).
* Timestamps remain ISO-8601 UTC **text** (`2026-01-31T09:15:00.000Z`): the API returns them
  verbatim and the business rules compare them as strings. Converting them to `timestamptz` would
  change API output and every comparison, so it is not part of this migration.
* PostgreSQL cannot store the NUL character in text or JSONB. NUL is removed from all request input
  at the boundary (JSON bodies, form fields, file names, query strings) on both databases, so they
  store identical text; the transfer refuses existing data that contains NUL (see below).

## Concurrency (multiple processes and servers)

On SQLite the application serialises writes with an in-process lock (single process only). On
PostgreSQL there is **no application lock**; every guarantee is enforced by the database:

| Guarantee | Mechanism |
|---|---|
| Two people editing the same record | `UPDATE ... WHERE revision = n` (optimistic locking) → one wins, the other gets 409 |
| Invoice number used once among issued/void invoices of an organization | unique partial index `ux_records_invoice_number` (migration 0004) |
| Payment reference used once per company (case-insensitive, rejected reports excluded) | unique partial index `ux_records_payment_reference` |
| A trial-balance file imported once per company, period and currency | unique partial index `ux_records_trialbalance_file` |
| Bulk "review required" flags (new document or trial-balance version) never overwrite a concurrent edit | the flagged rows are locked (`SELECT ... FOR UPDATE`) while rewritten |
| The workspace keeps at least one active Owner | the active Owner memberships are locked (fixed order) before the check |
| Over-reporting payments on one invoice | each report updates the invoice's revision, serialising reports per invoice |
| One scheduler pass at a time | lease row (`background_leases`) taken with `INSERT ... ON CONFLICT DO UPDATE ... WHERE` |
| One migration at a time | `pg_advisory_xact_lock` taken before Alembic runs (application start and `alembic` CLI) |
| Deadlock / lock timeout / serialization failure | returned as 409 "The workspace was busy... try again" |

A race that the database rejects returns **409** with the same message as the application check
(the sequential case keeps its original 400 response).

These are tested with separate OS processes (`backend/tests/test_postgres.py`): each worker builds
its own engine and pool and all are released together. The full compatibility suite (325 checks)
was also run against three `uvicorn --workers 3` processes sharing one PostgreSQL database, started
together on an empty database (migrations serialised by the advisory lock, one preview organization,
one reminder-lease holder, no duplicate reminders).

## Moving an existing SQLite workspace to PostgreSQL

The transfer copies everything and then **proves** the copy; it never changes the SQLite file.

1. **Stop the application** (no writes during the transfer).
2. **Back up** the SQLite database and the files directory, check the copy, and keep it:

   ```bash
   STAMP=$(date +%Y%m%d-%H%M%S); DATA=/var/lib/governance-desk; BACKUP=/var/backups/governance-desk
   mkdir -p "$BACKUP"
   sqlite3 "$DATA/preview.sqlite" ".backup '$BACKUP/preview-$STAMP.sqlite'"   # consistent online copy
   sqlite3 "$BACKUP/preview-$STAMP.sqlite" "PRAGMA integrity_check"           # must print: ok
   tar -C "$DATA" -czf "$BACKUP/files-$STAMP.tar.gz" files                   # uploaded documents
   ```
3. Create an empty PostgreSQL database and role.
4. Run the transfer from `backend/` (the URL comes from the environment, not the command line):

   ```bash
   export GOVERNANCE_DATABASE_URL='postgresql+psycopg://governance:...@db.internal:5432/governance'
   .venv/bin/python -m app.cli transfer-to-postgres \
       --source /var/lib/governance-desk/preview.sqlite --report transfer-report.json
   ```

   What it does:
   * snapshots the SQLite file with SQLite's backup API into a temporary directory, and migrates
     **the snapshot** to the latest revision;
   * migrates the empty PostgreSQL database to the latest revision (or checks it is already there)
     and refuses a database that already contains rows;
   * validates every record document (must be a JSON object) and every text column of every table
     (no NUL characters); any problem stops the transfer **before anything is written** and is
     listed in the report, so the data can be corrected in SQLite first;
   * copies every table in one transaction (all or nothing), preserving ids, revisions, timestamps,
     audit history, sessions, memberships and insertion order;
   * reconciles: row counts per table, organization and kind; every primary key; every column
     (record documents compared as parsed JSON); the order of every records list and audit trail.
5. Check the report: `"ok": true` and `"problems": []`. Exit status is 0 only in that case.
6. Point the application at PostgreSQL (`GOVERNANCE_DATABASE_URL`), keep the same
   `GOVERNANCE_DATA_DIR` (uploaded files stay where they are), start it and sign in.
7. Keep the SQLite file and the backup until the new system has been accepted.

Rollback: stop the application and unset `GOVERNANCE_DATABASE_URL`; the SQLite file is unchanged.
Anything written to PostgreSQL after the switch is not in the SQLite file.

Verified on: a copy of the local preview workspace (every table identical) and generated
workspaces with every record kind, files, invoices, payments and audit history; the application
then served the same state, portal, and file downloads from PostgreSQL (`test_transfer_*`).

## Running the tests on PostgreSQL

```bash
cd backend
# Uses GOVERNANCE_TEST_POSTGRES_URL if set (an admin URL; tests create and drop their own
# databases), otherwise starts a throwaway local cluster from the installed PostgreSQL binaries.
.venv/bin/python -m pytest tests/test_postgres.py          # PostgreSQL-specific tests
GOVERNANCE_TEST_DATABASE=postgres .venv/bin/python -m pytest  # the whole suite on PostgreSQL
```
