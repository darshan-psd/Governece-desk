"""Copy a Governance Desk SQLite database into an empty PostgreSQL database, then prove the copy.

``python -m app.cli transfer-to-postgres`` (see docs/POSTGRESQL.md):

1. **The source is never modified.** It is copied with SQLite's online backup API to a temporary
   file; the copy is migrated to the head revision (if it is behind) and read from there.
2. **The target** must be a PostgreSQL database at the head revision (an empty database is
   migrated first) with no application rows.
3. **Copy**: every table, in dependency order, inside one PostgreSQL transaction (all or
   nothing). The insertion order of records and audit entries (SQLite ``rowid``) becomes the
   ``seq`` column, so every list keeps its order. The data is validated first: a record whose
   data is not a JSON object, or any text containing a NUL character (PostgreSQL cannot store
   it), stops the transfer before anything is written (nothing is altered silently).
4. **Reconciliation**: row counts per table, per organization and per kind; every primary key;
   every column value (record documents compared as parsed JSON); the list order of every
   organization's records and audit trail. Foreign keys and constraints are checked by
   PostgreSQL while copying. Any difference makes the result ``ok: False``.

Uploaded files are not in the database: they stay in the files directory, so the application keeps
the same ``GOVERNANCE_DATA_DIR`` after the switch.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import sqlalchemy as sa

from app.database.migrations import current_revision, head_revision, upgrade_to_head
from app.models import Base

# Dependency order (PostgreSQL has foreign keys and triggers between these tables).
ORDER = ["organizations", "users", "records", "organization_memberships", "company_access", "audit", "sessions",
         "auth_tokens", "login_attempts", "security_events"]
# Short-lived coordination state that must not be carried over (a lease names a process of the old server).
SKIPPED = {"background_leases": "process leases are recreated by the running application"}
ROWID_TABLES = ("records", "audit")  # their SQLite rowid becomes the PostgreSQL seq column
BATCH = 1000


@dataclass
class TransferReport:
    ok: bool = False
    source_revision: str | None = None
    target_revision: str | None = None
    tables: dict[str, dict] = field(default_factory=dict)
    per_kind: dict[str, dict] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "source_revision": self.source_revision, "target_revision": self.target_revision,
                "tables": self.tables, "per_kind": self.per_kind, "skipped": self.skipped, "problems": self.problems}


def _tables() -> list[str]:
    known = [name for name in ORDER if name in Base.metadata.tables]
    later = sorted(name for name in Base.metadata.tables if name not in known and name not in SKIPPED)
    return known + later


def _snapshot(source: Path, workdir: Path) -> Path:
    """A consistent copy of the source taken with SQLite's backup API (safe while the app runs)."""
    copy = workdir / "source-copy.sqlite"
    reader = sqlite3.connect(f"file:{quote(str(source.resolve()))}?mode=ro", uri=True)
    writer = sqlite3.connect(copy)
    try:
        reader.backup(writer)
    finally:
        reader.close()
        writer.close()
    return copy


def _columns(connection, table: str) -> list[str]:
    return [column["name"] for column in sa.inspect(connection).get_columns(table)]


def _boolean_columns(connection, table: str) -> set[str]:
    """Columns the target declares as boolean. SQLite has no boolean type (it stores 0/1 as plain
    integers, and the raw DBAPI read here returns Python ``int``); PostgreSQL's driver refuses an
    ``int`` parameter for a ``boolean`` column outright, so these values are coerced to ``bool``
    before the row is sent."""
    return {c["name"] for c in sa.inspect(connection).get_columns(table) if isinstance(c["type"], sa.Boolean)}


def _validate_documents(source) -> list[str]:
    problems = []
    for owner, record_id, data in source.execute(sa.text("SELECT owner, id, data FROM records ORDER BY rowid")):
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            problems.append(f"record {owner}/{record_id}: data is not valid JSON")
            continue
        if not isinstance(parsed, dict):
            problems.append(f"record {owner}/{record_id}: data is not a JSON object")
        elif "\\u0000" in data or "\x00" in data:
            problems.append(f"record {owner}/{record_id}: data contains a NUL character (PostgreSQL cannot store it)")
    return problems


def _validate_text(source) -> list[str]:
    """PostgreSQL cannot store NUL in text: list every column of every table that contains one."""
    problems = []
    for table in _tables():
        for column in sa.inspect(source).get_columns(table):
            if any(name in str(column["type"]).upper() for name in ("TEXT", "CHAR", "CLOB")):
                count = source.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE instr({column['name']}, char(0)) > 0")).scalar()
                if count:
                    problems.append(f"{table}.{column['name']}: {count} row(s) contain a NUL character (PostgreSQL cannot store it)")
    return problems


def _rows(connection, table: str, columns: list[str], with_rowid: bool):
    selected = ", ".join([*columns, "rowid AS seq"] if with_rowid else columns)
    order = "rowid" if with_rowid else ", ".join(columns[:1])
    return connection.execute(sa.text(f"SELECT {selected} FROM {table} ORDER BY {order}")).mappings()


def _copy(source, target, report: TransferReport) -> None:
    for table in _tables():
        source_columns = set(_columns(source, table))
        target_columns = _columns(target, table)
        columns = [c for c in target_columns if c in source_columns and c != "seq"]
        missing = sorted(source_columns - set(target_columns))
        if missing:
            raise RuntimeError(f"target table {table} lacks column(s) {missing}")
        with_rowid = table in ROWID_TABLES
        insert_columns = [*columns, "seq"] if with_rowid else columns
        bool_columns = _boolean_columns(target, table) & set(columns)
        # JSON documents are sent as text and parsed by PostgreSQL (CAST ... AS JSONB).
        values = ", ".join(f"CAST(:{c} AS JSONB)" if (table, c) == ("records", "data") else f":{c}" for c in insert_columns)
        statement = sa.text(f"INSERT INTO {table} ({', '.join(insert_columns)}) VALUES ({values})")
        batch: list[dict] = []
        for row in _rows(source, table, columns, with_rowid):
            values_dict = dict(row)
            for name in bool_columns:
                if values_dict[name] is not None:
                    values_dict[name] = bool(values_dict[name])
            batch.append(values_dict)
            if len(batch) >= BATCH:
                target.execute(statement, batch)
                batch = []
        if batch:
            target.execute(statement, batch)
        if with_rowid:
            target.execute(sa.text(f"SELECT setval(pg_get_serial_sequence('{table}', 'seq'), COALESCE((SELECT MAX(seq) FROM {table}), 0) + 1, false)"))
    if "login_attempts" in _tables():
        target.execute(sa.text("SELECT setval(pg_get_serial_sequence('login_attempts', 'id'), "
                               "COALESCE((SELECT MAX(id) FROM login_attempts), 0) + 1, false)"))


def _normalise(table: str, row: dict) -> dict:
    if table == "records" and row.get("data") is not None:
        return {**row, "data": json.loads(row["data"])}
    return row


def reconcile(source, target, report: TransferReport) -> None:
    """Compares every copied table row by row; appends every difference to ``report.problems``."""
    for table in _tables():
        columns = [c for c in _columns(target, table) if c in set(_columns(source, table)) and c != "seq"]
        keys = sa.inspect(target).get_pk_constraint(table)["constrained_columns"]
        source_rows = [dict(r) for r in source.execute(sa.text(f"SELECT {', '.join(columns)} FROM {table}")).mappings()]
        cast = [f"CAST({c} AS TEXT) AS {c}" if (table, c) == ("records", "data") else c for c in columns]
        target_rows = [dict(r) for r in target.execute(sa.text(f"SELECT {', '.join(cast)} FROM {table}")).mappings()]
        matched = 0
        source_by_key = {tuple(r[k] for k in keys): r for r in source_rows}
        target_by_key = {tuple(r[k] for k in keys): r for r in target_rows}
        for key in sorted(set(source_by_key) - set(target_by_key), key=str)[:20]:
            report.problems.append(f"{table}: row {key} missing in PostgreSQL")
        for key in sorted(set(target_by_key) - set(source_by_key), key=str)[:20]:
            report.problems.append(f"{table}: unexpected row {key} in PostgreSQL")
        for key, row in source_by_key.items():
            other = target_by_key.get(key)
            if other is None:
                continue
            if _normalise(table, row) == _normalise(table, other):
                matched += 1
            elif len(report.problems) < 200:
                differing = sorted(c for c in columns if _normalise(table, row).get(c) != _normalise(table, other).get(c))
                report.problems.append(f"{table}: row {key} differs in {differing}")
        report.tables[table] = {"source": len(source_rows), "target": len(target_rows), "identical": matched}
        if not (len(source_rows) == len(target_rows) == matched):
            report.problems.append(f"{table}: {len(source_rows)} source rows, {len(target_rows)} copied, {matched} identical")

    for owner, kind, count in source.execute(sa.text("SELECT owner, kind, COUNT(*) FROM records GROUP BY owner, kind")):
        report.per_kind.setdefault(owner, {})[kind] = {"source": count}
    for owner, kind, count in target.execute(sa.text("SELECT owner, kind, COUNT(*) FROM records GROUP BY owner, kind")):
        report.per_kind.setdefault(owner, {}).setdefault(kind, {})["target"] = count
    for owner, kinds in report.per_kind.items():
        for kind, counts in kinds.items():
            if counts.get("source") != counts.get("target"):
                report.problems.append(f"records of kind {kind} in {owner}: {counts.get('source')} vs {counts.get('target')}")

    # The order every list is shown in (newest first, ties in insertion order) must not change.
    for table, sql_source, sql_target in (
        ("records", "SELECT owner, id FROM records ORDER BY owner, updated DESC, kind, rowid",
         'SELECT owner, id FROM records ORDER BY owner COLLATE "C", updated DESC, kind, seq'),
        ("audit", "SELECT owner, id FROM audit ORDER BY owner, at DESC, rowid DESC",
         'SELECT owner, id FROM audit ORDER BY owner COLLATE "C", at DESC, seq DESC'),
    ):
        if list(source.execute(sa.text(sql_source))) != list(target.execute(sa.text(sql_target))):
            report.problems.append(f"{table}: list order differs after the transfer")


def _target_is_empty(target) -> list[str]:
    return [table for table in _tables() + list(SKIPPED)
            if target.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar()]


def transfer(source_path: Path, target_url: str) -> TransferReport:
    report = TransferReport(skipped=dict(SKIPPED))
    if not source_path.is_file():
        report.problems.append("The source SQLite database does not exist.")
        return report
    target_engine = sa.create_engine(target_url, poolclass=sa.pool.NullPool)
    if target_engine.dialect.name != "postgresql":
        report.problems.append("The target must be a PostgreSQL database.")
        return report
    workdir = Path(tempfile.mkdtemp(prefix="gd-transfer-"))
    try:
        copy = _snapshot(source_path, workdir)
        source_engine = sa.create_engine(f"sqlite:///{copy}", poolclass=sa.pool.NullPool)
        upgrade_to_head(source_engine)  # the temporary copy only
        report.source_revision = current_revision(source_engine)
        if current_revision(target_engine) is None:
            upgrade_to_head(target_engine)
        report.target_revision = current_revision(target_engine)
        if report.target_revision != head_revision():
            report.problems.append(f"The target is at revision {report.target_revision}, expected {head_revision()}. Run the migrations first.")
            return report
        with source_engine.connect() as source:
            report.problems += _validate_documents(source) + _validate_text(source)
            if report.problems:
                return report
            with target_engine.begin() as target:
                occupied = _target_is_empty(target)
                if occupied:
                    report.problems.append(f"The target already contains data in {occupied}; transfer into an empty database.")
                    return report
                _copy(source, target, report)
            with target_engine.connect() as target:
                reconcile(source, target, report)
        source_engine.dispose()
        report.ok = not report.problems
        return report
    finally:
        target_engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)
