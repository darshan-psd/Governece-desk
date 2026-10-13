"""Database engine, sessions and transactions (SQLite or PostgreSQL)."""
from __future__ import annotations

import contextlib
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.database.sqlite import enable_foreign_keys

# PostgreSQL errors that mean "another transaction got there first": the request can be retried.
_RETRYABLE_SQLSTATES = {
    "40001": "serialization_failure",
    "40P01": "deadlock_detected",
    "55P03": "lock_not_available",
}
BUSY_MESSAGE = "The workspace was busy with another change. Reload and try again."


class Database:
    """Owns the engine and hands out read sessions and atomic write transactions.

    * **SQLite** (development and tests): writes are serialised with a process-wide lock, as the
      Node backend did; uniqueness and revision checks run against fresh data inside the lock.
      Single process only.
    * **PostgreSQL** (staging/production, any number of processes): no application lock. Every
      guarantee is enforced by the database itself: conditional ``UPDATE ... WHERE revision = n``
      (optimistic locking), unique indexes for invoice numbers and payment references, row locks
      (``FOR UPDATE``) where a decision reads several rows, and an advisory lock for migrations.
    """

    def __init__(self, url: str | Path, settings=None):
        if isinstance(url, Path):
            url = f"sqlite:///{url}"
        self.url = make_url(url)
        self.dialect = self.url.get_backend_name()
        if self.dialect == "sqlite":
            self.path = Path(self.url.database or "")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
            # Foreign keys (identity/tenancy tables) are enforced on every connection.
            enable_foreign_keys(self.engine)
            self._write_lock = threading.RLock()
        elif self.dialect == "postgresql":
            self.path = None
            s = settings
            options = " ".join([
                f"-c statement_timeout={s.governance_db_statement_timeout_ms if s else 30000}",
                f"-c lock_timeout={s.governance_db_lock_timeout_ms if s else 10000}",
                "-c idle_in_transaction_session_timeout=60000",
            ])
            self.engine = create_engine(
                url,
                pool_size=s.governance_db_pool_size if s else 5,
                max_overflow=s.governance_db_max_overflow if s else 5,
                pool_timeout=s.governance_db_pool_timeout if s else 10,
                pool_recycle=s.governance_db_pool_recycle if s else 1800,
                pool_pre_ping=True,  # replaces connections the server or a proxy closed
                connect_args={"options": options, "application_name": "governance-desk"},
            )
            self._write_lock = None
        else:
            raise ValueError(f"Unsupported database dialect: {self.dialect}")
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgresql"

    def prepare_schema(self, auto_migrate: bool) -> None:
        """Brings the schema to the latest Alembic revision, or checks that it already is.

        ``auto_migrate`` (development/test): apply pending migrations; an existing pre-Alembic
        database is adopted without touching its rows. Otherwise (staging/production): refuse to
        start unless ``alembic upgrade head`` has been run as a deployment step.
        """
        from app.database.migrations import upgrade_to_head, verify_at_head

        if auto_migrate:
            upgrade_to_head(self.engine)
        else:
            verify_at_head(self.engine)

    def ping(self) -> None:
        """Readiness probe: a trivial query on a fresh connection."""
        with self.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")

    @contextmanager
    def read(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def write(self) -> Iterator[Session]:
        """One atomic write transaction: commits on success, rolls back on any exception."""
        with self._write_lock or contextlib.nullcontext():
            session = self._sessions()
            try:
                yield session
                session.commit()
            except DBAPIError as error:
                session.rollback()
                sqlstate = getattr(error.orig, "sqlstate", None)
                if sqlstate in _RETRYABLE_SQLSTATES:
                    from app.core.errors import Conflict

                    raise Conflict(BUSY_MESSAGE) from error
                raise
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()

    def dispose(self) -> None:
        self.engine.dispose()
