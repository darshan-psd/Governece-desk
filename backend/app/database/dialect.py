"""The few SQL constructs that differ between SQLite and PostgreSQL, compiled per dialect.

Everything else in the repositories is portable SQLAlchemy. Keeping the differences here means
the services and business rules are identical on both databases:

* ``JsonDocument``: a record's JSON document. ``TEXT`` on SQLite, ``JSONB`` on PostgreSQL; Python
  always writes and reads JSON text, so the repository contract does not change.
* ``json_text(column, field)``: a top-level field of a JSON document as SQL text-or-scalar
  (``json_extract(col, '$.f')`` / ``(col ->> 'f')``). Literal paths, so expression indexes match.
* ``insertion_order(table)``: stable insertion order (SQLite ``rowid`` / PostgreSQL ``seq``
  identity column), the tie-breaker of every list order.
* ``owner_column(rowid_driven)``: SQLite's ``+records.owner`` planner hint, plain elsewhere.
* ``insert_for(session, table)``: the dialect's INSERT supporting ``ON CONFLICT``.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement
from sqlalchemy.types import TypeDecorator, UserDefinedType

POSTGRES = "postgresql"


def name_of(bind) -> str:
    """Dialect name of a Session, Connection or Engine."""
    return bind.get_bind().dialect.name if hasattr(bind, "get_bind") else bind.dialect.name


class _Jsonb(UserDefinedType):
    """JSONB column that takes and returns JSON *text* (no client-side parsing or re-serialising)."""

    cache_ok = True

    def get_col_spec(self, **_kw) -> str:
        return "JSONB"

    def bind_processor(self, dialect):
        return None  # psycopg sends str with an unknown type; PostgreSQL parses it as jsonb

    def result_processor(self, dialect, coltype):
        return None

    def column_expression(self, colexpr):
        return sa.cast(colexpr, sa.Text)


class JsonDocument(TypeDecorator):
    impl = sa.Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(_Jsonb() if dialect.name == POSTGRES else sa.Text())


class json_text(ColumnElement):
    """Top-level field ``name`` of the JSON document in ``column`` (``name`` is validated by callers).

    For WHERE / ORDER BY. To select it, give it a label: ``json_text(col, "task").label("task")``.
    """

    inherit_cache = True
    type = sa.Text()

    # Cache key: the column and the field name (both change the SQL text).
    _traverse_internals = [("column", sa.sql.visitors.InternalTraversal.dp_clauseelement),
                           ("name", sa.sql.visitors.InternalTraversal.dp_string)]

    def __init__(self, column, name: str):
        self.column = column
        self.name = name


@compiles(json_text)
def _json_text_sqlite(element, compiler, **kw):
    return f"json_extract({compiler.process(element.column, **kw)}, '$.{element.name}')"


@compiles(json_text, POSTGRES)
def _json_text_postgres(element, compiler, **kw):
    return f"({compiler.process(element.column, **kw)} ->> '{element.name}')"


class insertion_order(ColumnElement):
    """Insertion order of the rows of ``table`` (``records`` or ``audit``)."""

    inherit_cache = True
    type = sa.BigInteger()
    _traverse_internals = [("table", sa.sql.visitors.InternalTraversal.dp_string)]

    def __init__(self, table: str):
        self.table = table


@compiles(insertion_order)
def _order_sqlite(element, compiler, **kw):
    return f"{element.table}.rowid"


@compiles(insertion_order, POSTGRES)
def _order_postgres(element, compiler, **kw):
    return f"{element.table}.seq"


class owner_column(ColumnElement):
    """``records.owner``; on SQLite with the unary ``+`` that keeps the owner index from driving
    a query whose selective ``rowid IN (...)`` condition should drive it instead."""

    inherit_cache = True
    type = sa.Text()
    _traverse_internals = [("hint", sa.sql.visitors.InternalTraversal.dp_boolean)]

    def __init__(self, hint: bool):
        self.hint = hint


@compiles(owner_column)
def _owner_sqlite(element, compiler, **kw):
    return "+records.owner" if element.hint else "records.owner"


@compiles(owner_column, POSTGRES)
def _owner_postgres(element, compiler, **kw):
    return "records.owner"


def insert_for(bind, table):
    """INSERT with ``on_conflict_do_nothing`` / ``on_conflict_do_update`` for the bind's dialect."""
    return pg_insert(table) if name_of(bind) == POSTGRES else sqlite_insert(table)


def json_array_contains(dialect: str, column_sql: str, field: str, param: str) -> str:
    """SQL text: the JSON array ``field`` of ``column_sql`` contains the string bound as ``:param``."""
    if dialect == POSTGRES:
        return (f"jsonb_typeof({column_sql} -> '{field}') = 'array' AND "
                f"({column_sql} -> '{field}') @> jsonb_build_array(CAST(:{param} AS TEXT))")
    # json_type guard: only real arrays count, exactly like the Python ``as_list()`` check.
    return (f"json_type({column_sql}, '$.{field}') = 'array' AND "
            f"EXISTS (SELECT 1 FROM json_each({column_sql}, '$.{field}') WHERE json_each.value = :{param})")
