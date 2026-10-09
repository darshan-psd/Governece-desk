"""Alembic integration: apply migrations (development/test) or verify them (staging/production)."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from app.core.config import BACKEND_DIR, ConfigurationError


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade_to_head(engine: Engine) -> None:
    """Applies pending migrations in one transaction (adopts an existing pre-Alembic database)."""
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def verify_at_head(engine: Engine) -> None:
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise ConfigurationError(
            f"Database schema is at revision {current or 'none'}, expected {head}. "
            "Run `alembic upgrade head` before starting the application.")
