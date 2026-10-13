"""PostgreSQL for the test suite.

``GOVERNANCE_TEST_POSTGRES_URL`` (e.g. a CI service container) is used when set: it must be an
administrator URL that may create and drop databases. Otherwise a throwaway cluster is started from
the local PostgreSQL binaries (``initdb``) in a temporary directory, listening on 127.0.0.1 only,
and removed afterwards. Tests never touch any other database.
"""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa

CANDIDATE_BIN_DIRS = [
    *sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"), reverse=True),
    *sorted(glob.glob("/usr/local/opt/postgresql@*/bin"), reverse=True),
    *sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True),
]


def _bin_dir() -> Path | None:
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    for candidate in CANDIDATE_BIN_DIRS:
        if (Path(candidate) / "initdb").is_file():
            return Path(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TemporaryPostgres:
    """A private PostgreSQL cluster for one test session (trust auth, loopback TCP only)."""

    def __init__(self):
        self.bin = _bin_dir()
        self.directory: Path | None = None
        self.port = _free_port()

    @property
    def available(self) -> bool:
        return self.bin is not None

    def start(self) -> str:
        self.directory = Path(tempfile.mkdtemp(prefix="gd-pg-"))
        data = self.directory / "data"
        subprocess.run([str(self.bin / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--locale=C"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        options = f"-p {self.port} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off -c max_connections=200"
        subprocess.run([str(self.bin / "pg_ctl"), "-D", str(data), "-o", options, "-l", str(self.directory / "log.txt"), "-w", "start"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/postgres"

    def stop(self) -> None:
        if self.directory is not None:
            subprocess.run([str(self.bin / "pg_ctl"), "-D", str(self.directory / "data"), "-m", "immediate", "stop"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(self.directory, ignore_errors=True)


class DatabaseFactory:
    """Creates uniquely named databases on one server and drops them again."""

    def __init__(self, admin_url: str):
        self.admin_url = admin_url
        self.engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
        self.template: str | None = None

    def url_for(self, name: str) -> str:
        return sa.engine.make_url(self.admin_url).set(database=name).render_as_string(hide_password=False)

    def create(self, template: str | None = None) -> str:
        name = f"gdt_{uuid.uuid4().hex[:12]}"
        with self.engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
        return name

    def drop(self, name: str) -> None:
        with self.engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def migrated_template(self) -> str:
        """A database at the head revision, copied (fast) for every test that needs one."""
        if self.template is None:
            from app.database.migrations import upgrade_to_head

            name = self.create()
            engine = sa.create_engine(self.url_for(name), poolclass=sa.pool.NullPool)
            upgrade_to_head(engine)
            engine.dispose()
            self.template = name
        return self.template

    def close(self) -> None:
        if self.template:
            self.drop(self.template)
        self.engine.dispose()


def admin_url_from_environment() -> str | None:
    return os.environ.get("GOVERNANCE_TEST_POSTGRES_URL") or None
