"""SQLite engine, sessions and transactions."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker



class Database:
    """Owns the engine. Writes are serialised with a process-wide lock.

    The Node backend ran each request's writes as one atomic batch on a single thread. Holding
    one lock per write transaction gives the same guarantees here: uniqueness checks (invoice
    numbers, payment references) and revision checks run against fresh data inside the
    transaction. Multi-process deployments will need database-level locking (PostgreSQL phase).
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(connection, _record):  # pragma: no cover - trivial
            connection.execute("PRAGMA busy_timeout = 30000")

        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._write_lock = threading.RLock()

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
        with self._write_lock:
            session = self._sessions()
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()

    def dispose(self) -> None:
        self.engine.dispose()
