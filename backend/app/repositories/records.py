"""Data access for workspace records and the audit log.

Every query is scoped to one workspace (``owner``). Services receive plain dicts shaped like
the Node backend's records: ``{...data, id, kind, revision, updated}``.

Records keep their fields in the JSON ``data`` column (TEXT on SQLite, JSONB on PostgreSQL).
Filters on those fields use ``app.models.json_field`` with a literal path, so they can use the
expression indexes declared in ``app.models`` (a bound-parameter path would not match an index).
The SQL that differs between the two databases lives in ``app.database.dialect``.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterable, Sequence

from sqlalchemy import ColumnElement, Text, and_, case, cast, func, literal_column, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.core.jsvalues import now_iso, without_nul
from app.database.dialect import POSTGRES, insert_for, insertion_order, json_array_contains, name_of, owner_column
from app.models import AuditEvent, Record, json_field

# Columns overlay the JSON fields, exactly like the Node backend's row mapping.
COLUMN_KEYS = ("id", "kind", "revision", "updated")
COLUMNS = (Record.id, Record.kind, Record.data, Record.revision, Record.updated)
ROWID = insertion_order("records")  # rowid on SQLite, the seq identity column on PostgreSQL
# Newest first. Ties keep the order the original query produced (it read rows through the
# (owner, kind) index, so equal timestamps came out by kind, then insertion order).
ORDER = (Record.updated.desc(), Record.kind.asc(), ROWID.asc())
# SQLite allows at most 32766 bound parameters; batches stay far below that.
BATCH = 500
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# Business uniqueness enforced by the database (migration 0004): index name → conflict message.
UNIQUE_RULES = {
    "ux_records_invoice_number": "Invoice number already issued or record changed.",
    "ux_records_payment_reference": "This transaction reference has already been reported.",
    "ux_records_trialbalance_file": "This file has already been imported for this company, period and currency.",
}


def business_conflict(error: IntegrityError) -> Conflict | None:
    """The 409 for a violated business uniqueness index, or None for any other integrity error."""
    message = str(getattr(error, "orig", error))
    for index, text_ in UNIQUE_RULES.items():
        if index in message:
            return Conflict(text_)
    return None


def dumps(data: Any) -> str:
    """Compact JSON. NUL characters are removed from text (PostgreSQL JSONB cannot store them, and
    no business field can legitimately contain one), so both databases store the same document."""
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if "\\u0000" in encoded:
        encoded = json.dumps(without_nul(data), separators=(",", ":"), ensure_ascii=False)
    return encoded


def row_to_dict(record_id: str, kind: str, data: str, revision: int, updated: str) -> dict:
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        parsed = {}
    return {**parsed, "id": record_id, "kind": kind, "revision": revision, "updated": updated}


def to_dict(row: Record) -> dict:
    return row_to_dict(row.id, row.kind, row.data, row.revision, row.updated)


def field(name: str) -> ColumnElement:
    """SQL expression for a top-level JSON field of a record (or one of its columns)."""
    if name in ("id", "kind", "revision", "updated"):
        return getattr(Record, name)
    if not _FIELD_RE.match(name):
        raise ValueError(f"invalid field name: {name}")
    return json_field(name)


def _chunks(values: Sequence, size: int = BATCH):
    for start in range(0, len(values), size):
        yield values[start:start + size]


class RecordRepository:
    def __init__(self, session: Session, owner: str):
        self.session = session
        self.owner = owner
        # One timestamp per transaction, like the Node backend's batched statements; rows written
        # together keep their insertion order when lists are sorted by ``updated``.
        self.stamp = now_iso()

    # Query building ------------------------------------------------------------------------
    def _conditions(self, *, kinds: Iterable[str] | None = None, exclude_kinds: Iterable[str] | None = None,
                    ids: Iterable[str] | None = None, where: dict[str, Any] | None = None,
                    extra: Iterable[ColumnElement] = (), rowid_driven: bool = False) -> list[ColumnElement]:
        """``where`` maps a field to a value (equality) or to a list/tuple/set (IN).

        ``rowid_driven``: an ``extra`` condition is a selective ``rowid IN (subquery)``. The owner
        test is then written ``+owner`` (SQLite's documented way to keep a term from choosing the
        index) so the rowid list drives the query instead of a scan of the whole workspace.
        """
        owner = owner_column(True) if rowid_driven else Record.owner
        conditions: list[ColumnElement] = [owner == self.owner]
        if kinds is not None:
            kinds = list(kinds)
            conditions.append(Record.kind == kinds[0] if len(kinds) == 1 else Record.kind.in_(kinds))
        if exclude_kinds:
            conditions.append(Record.kind.not_in(list(exclude_kinds)))
        if ids is not None:
            conditions.append(Record.id.in_(list(ids)))
        for name, value in (where or {}).items():
            column = field(name)
            if isinstance(value, (list, tuple, set, frozenset)):
                conditions.append(column.in_(list(value)))
            else:
                conditions.append(column == value)
        conditions.extend(extra)
        return conditions

    def _select(self, columns, conditions, *, order: bool = True, limit: int | None = None, offset: int = 0, order_by=None):
        statement = select(*columns).where(and_(*conditions))
        if order_by is not None:
            statement = statement.order_by(*order_by)
        elif order:
            statement = statement.order_by(*ORDER)
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        return statement

    # Reads -------------------------------------------------------------------------------
    def find(self, *, order: bool = True, limit: int | None = None, offset: int = 0, order_by=None, **filters) -> list[dict]:
        """Records matching the filters, filtered and ordered by the database (newest first by default)."""
        statement = self._select(COLUMNS, self._conditions(**filters), order=order, limit=limit, offset=offset, order_by=order_by)
        return [row_to_dict(*row) for row in self.session.execute(statement)]

    def count(self, **filters) -> int:
        return self.session.execute(select(func.count()).select_from(Record).where(and_(*self._conditions(**filters)))).scalar_one()

    def exists(self, **filters) -> bool:
        statement = select(literal_column("1")).select_from(Record).where(and_(*self._conditions(**filters))).limit(1)
        return self.session.execute(statement).first() is not None

    def all(self) -> list[dict]:
        return self.find()

    def by_kind(self, kind: str) -> list[dict]:
        return self.find(kinds=[kind])

    def get(self, record_id: Any) -> dict | None:
        if not isinstance(record_id, str):
            return None
        row = self.session.execute(select(*COLUMNS).where(Record.owner == self.owner, Record.id == record_id)).first()
        return row_to_dict(*row) if row else None

    def get_many(self, ids: Iterable[Any]) -> dict[str, dict]:
        """Primary-key lookups in batches: ``{id: record}`` for the ids that exist."""
        wanted = sorted({i for i in ids if isinstance(i, str)})
        found: dict[str, dict] = {}
        for chunk in _chunks(wanted):
            for row in self.session.execute(select(*COLUMNS).where(Record.owner == self.owner, Record.id.in_(chunk))):
                found[row[0]] = row_to_dict(*row)
        return found

    def json_rows(self, **filters) -> list[tuple[str, str, str]]:
        """``(kind, id, record JSON)`` with the column fields merged and ``key`` removed by the database.

        Used by full-state responses so Python does not parse and re-serialise every record.
        """
        if name_of(self.session) == POSTGRES:
            # JSONB is always parsed: drop "key", overlay the column fields, render as text.
            base = case((func.jsonb_typeof(Record.data) != "object", literal_column("'{}'::jsonb")),
                        else_=Record.data.op("-")(literal_column("'key'")))
            overlay = func.jsonb_build_object(literal_column("'id'"), Record.id, literal_column("'kind'"), Record.kind,
                                              literal_column("'revision'"), Record.revision, literal_column("'updated'"), Record.updated)
            record_json = cast(base.op("||")(overlay), Text)
            statement = self._select((Record.kind, Record.id, record_json), self._conditions(**filters))
            return [tuple(row) for row in self.session.execute(statement)]
        # Non-object data becomes {} (as row_to_dict does) and ``key`` (storage path) is never
        # returned. Both tests avoid a full JSON parse where possible: stored objects start with
        # "{", and json_remove only runs when the text mentions "key" at all.
        first_char = func.substr(func.ltrim(Record.data, literal_column("' ' || char(9, 10, 13)")), 1, 1)
        base = case(
            (first_char != "{", literal_column("'{}'")),
            (func.instr(Record.data, '"key"') > 0, func.json_remove(Record.data, literal_column("'$.key'"))),
            else_=Record.data,
        )
        record_json = func.json_set(
            base,
            literal_column("'$.id'"), Record.id, literal_column("'$.kind'"), Record.kind,
            literal_column("'$.revision'"), Record.revision, literal_column("'$.updated'"), Record.updated,
        )
        statement = self._select((Record.kind, Record.id, record_json), self._conditions(**filters))
        return [tuple(row) for row in self.session.execute(statement)]

    # Writes ------------------------------------------------------------------------------
    def insert(self, record_id: str, kind: str, data: dict) -> None:
        self.session.add(
            Record(owner=self.owner, id=record_id, kind=kind, data=dumps(data), revision=1, updated=self.stamp)
        )
        try:
            self.session.flush()
        except IntegrityError as error:
            raise (business_conflict(error) or BadRequest("A record with this identifier already exists.")) from error

    def insert_or_ignore(self, record_id: str, kind: str, data: dict) -> bool:
        statement = (
            insert_for(self.session, Record)
            .values(owner=self.owner, id=record_id, kind=kind, data=dumps(data), revision=1, updated=self.stamp)
            .on_conflict_do_nothing()
            .returning(Record.id)  # rowcount is not reliable for INSERT on every driver
        )
        return self.session.execute(statement).first() is not None

    def update_if_revision(self, record_id: str, revision: int, data: dict, touch: bool = True) -> bool:
        """Optimistic locking: writes only when the stored revision still matches."""
        values: dict[str, Any] = {"data": dumps(data), "revision": Record.revision + 1}
        if touch:
            values["updated"] = self.stamp
        try:
            result = self.session.execute(
                update(Record)
                .where(Record.owner == self.owner, Record.id == record_id, Record.revision == revision)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        except IntegrityError as error:
            conflict = business_conflict(error)
            if conflict is None:
                raise
            raise conflict from error
        return result.rowcount == 1

    def patch_where(self, kinds: Iterable[str], patch: dict, *, where: dict[str, Any] | None = None,
                    evidence_contains: str | None = None) -> int:
        """Merges ``patch`` into every matching record (revision + 1, ``updated`` unchanged).

        Mirrors the Node backend's ``json_set`` bulk updates. Matching happens in the database.
        The rows are locked while they are rewritten (``FOR UPDATE`` on PostgreSQL; SQLite's writer
        lock already serialises writes), so a concurrent edit is never overwritten: it either commits
        first and is merged into, or waits and then fails its revision check (409).
        """
        extra = [evidence_sql(evidence_contains, name_of(self.session))] if evidence_contains is not None else []
        rows = self.session.execute(select(Record.id, Record.data, Record.revision).where(
            and_(*self._conditions(kinds=kinds, where=where, extra=extra))).with_for_update()).all()
        for record_id, data, revision in rows:
            merged = json.loads(data)
            merged.update(patch)
            self.session.execute(
                update(Record).where(Record.owner == self.owner, Record.id == record_id)
                .values(data=dumps(merged), revision=revision + 1).execution_options(synchronize_session=False)
            )
        return len(rows)


def evidence_sql(document_id: str, dialect: str = "sqlite") -> ColumnElement:
    """True when the record's ``evidence`` array contains ``document_id`` (only real arrays count,
    exactly like the Python ``as_list()`` check)."""
    return text(json_array_contains(dialect, "records.data", "evidence", "evidence_doc")).bindparams(evidence_doc=document_id)


class AuditRepository:
    # Newest first; equal timestamps in reverse insertion order (the audit_owner_at index scanned backwards).
    ORDER = (AuditEvent.at.desc(), insertion_order("audit").desc())

    def __init__(self, session: Session, owner: str, actor_membership_id: str | None = None):
        self.session = session
        self.owner = owner
        self.actor_membership_id = actor_membership_id

    @classmethod
    def of(cls, session: Session, ctx) -> "AuditRepository":
        """Audit writer for a request: the organization and acting membership come from the session."""
        return cls(session, ctx.owner, getattr(ctx, "membership_id", None))

    def add(self, action: str, subject: Any) -> None:
        self.session.add(
            AuditEvent(id=str(uuid.uuid4()), owner=self.owner, action=action,
                       subject="" if subject is None else str(subject), at=now_iso(),
                       actor_membership_id=self.actor_membership_id)
        )
        self.session.flush()

    def recent(self, limit: int = 300, offset: int = 0) -> list[dict]:
        rows = self.session.execute(
            select(AuditEvent.id, AuditEvent.owner, AuditEvent.action, AuditEvent.subject, AuditEvent.at)
            .where(AuditEvent.owner == self.owner).order_by(*self.ORDER).limit(limit).offset(offset)
        )
        return [{"id": r[0], "owner": r[1], "action": r[2], "subject": r[3], "at": r[4]} for r in rows]

    def count(self) -> int:
        return self.session.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.owner == self.owner)).scalar_one()
