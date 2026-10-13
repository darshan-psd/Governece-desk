"""Alembic integration: apply migrations (development/test) or verify them (staging/production)."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import BACKEND_DIR, ConfigurationError
from app.database.sqlite import transactional_engine

# Any constant: every process upgrading the same PostgreSQL database waits on this lock, so two
# application processes (or a deploy job and an app) can never run migrations concurrently.
MIGRATION_LOCK_ID = 7_314_202_610


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def run_upgrade(connection, target: str = "head") -> None:
    """Applies pending migrations on an open connection inside its transaction."""
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, target)


def upgrade_to_head(engine: Engine, target: str = "head") -> None:
    """Applies pending migrations in ONE transaction, DDL included: either every pending revision
    is applied or the database is left exactly as it was (SQLite and PostgreSQL both run DDL
    transactionally)."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            run_upgrade(connection, target)
        return
    migration_engine = transactional_engine(engine.url)
    try:
        with migration_engine.begin() as connection:
            run_upgrade(connection, target)
    finally:
        migration_engine.dispose()


def verify_at_head(engine: Engine) -> None:
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise ConfigurationError(
            f"Database schema is at revision {current or 'none'}, expected {head}. "
            "Run `alembic upgrade head` before starting the application.")
