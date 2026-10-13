"""SQLite connection settings shared by the application and migration engines."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def enable_foreign_keys(engine: Engine) -> None:
    """SQLite ignores foreign keys unless enabled per connection (before any transaction)."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA busy_timeout = 30000")


def transactional_engine(url) -> Engine:
    """Engine whose transactions also cover DDL, so a failed migration rolls back completely.

    Python's sqlite3 driver normally commits before DDL; taking over BEGIN (the documented
    SQLAlchemy recipe) makes CREATE/ALTER part of the same transaction as the data changes.
    """
    engine = create_engine(url, connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None  # driver autocommit; we issue BEGIN ourselves
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA busy_timeout = 30000")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
