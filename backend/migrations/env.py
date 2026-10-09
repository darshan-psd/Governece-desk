"""Alembic environment.

Two entry points:
* the ``alembic`` CLI: the database comes from ``-x db=<path>`` or the application settings;
* the application (``app.database.migrations``): it passes an open connection.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.models import Base

config = context.config
target_metadata = Base.metadata
connection = config.attributes.get("connection")

if connection is None and config.config_file_name is not None:
    # Only the CLI configures logging here; the application already has its own logging set up.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("db")
    if override:
        return f"sqlite:///{override}"
    from app.core.config import get_settings

    return f"sqlite:///{get_settings().database_path}"


def run(conn) -> None:
    # render_as_batch: SQLite cannot ALTER most things in place; batch mode rebuilds tables safely.
    context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    run(connection)
else:
    engine = create_engine(database_url())
    with engine.begin() as conn:
        run(conn)
    engine.dispose()
