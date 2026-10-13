"""Alembic environment.

Two entry points:
* the ``alembic`` CLI: the database comes from ``-x db=<sqlite path>``, ``-x url=<database URL>``
  or the application settings (``GOVERNANCE_DATABASE_URL``, else the SQLite file);
* the application (``app.database.migrations``): it passes an open connection.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from app.models import Base

config = context.config
target_metadata = Base.metadata
connection = config.attributes.get("connection")

if connection is None and config.config_file_name is not None:
    # Only the CLI configures logging here; the application already has its own logging set up.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    arguments = context.get_x_argument(as_dictionary=True)
    if arguments.get("url"):
        return arguments["url"]
    if arguments.get("db"):
        return f"sqlite:///{arguments['db']}"
    from app.core.config import get_settings

    return get_settings().database_url


def run(conn) -> None:
    # render_as_batch: SQLite cannot ALTER most things in place; batch mode rebuilds tables safely.
    # transactional_ddl: the connection runs DDL inside the transaction (SQLite via
    # transactional_engine, PostgreSQL natively), so all pending revisions apply atomically.
    context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=conn.dialect.name == "sqlite",
                      compare_type=True, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    run(connection)
else:
    from sqlalchemy import create_engine, text

    from app.database.migrations import MIGRATION_LOCK_ID
    from app.database.sqlite import transactional_engine

    url = database_url()
    engine = transactional_engine(url) if url.startswith("sqlite") else create_engine(url)
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        run(conn)
    engine.dispose()
