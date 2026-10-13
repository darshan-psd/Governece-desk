"""Test fixtures: an isolated FastAPI app per test with its own database and document folder.

The database is a SQLite file by default. ``GOVERNANCE_TEST_DATABASE=postgres`` runs the whole
suite on PostgreSQL instead: every test gets its own database (see ``tests/postgres.py``).
"""
from __future__ import annotations

import os
from pathlib import Path

# Tests must not depend on the developer's shell: drop inherited GOVERNANCE_* settings before the
# app module is imported (it builds an app object at import time). Fixtures use the "test" profile.
_KEEP = ("GOVERNANCE_E2E", "GOVERNANCE_TEST_DATABASE", "GOVERNANCE_TEST_POSTGRES_URL")
for _key in [k for k in os.environ if k.startswith("GOVERNANCE_") and k not in _KEEP]:
    del os.environ[_key]
TEST_DATABASE = os.environ.get("GOVERNANCE_TEST_DATABASE", "sqlite")

import pytest
from fastapi.testclient import TestClient

from app.auth import csrf_for
from app.core.config import Settings
from app.main import create_app
from tests.postgres import DatabaseFactory, TemporaryPostgres, admin_url_from_environment

COOKIE = "gov_preview"
CSRF_COOKIE = "gov_csrf"
ANONYMOUS_CSRF = "anonymous-csrf-token-for-tests"
SAFE = ("GET", "HEAD", "OPTIONS")


def browser_headers(method: str, session: str | None = None, cookie_name: str = COOKIE) -> dict:
    """Cookie + X-CSRF-Token headers exactly as the React app sends them."""
    csrf = csrf_for(session) if session else ANONYMOUS_CSRF
    cookies = ([f"{cookie_name}={session}"] if session else []) + [f"{CSRF_COOKIE}={csrf}"]
    headers = {"cookie": "; ".join(cookies)}
    if method.upper() not in SAFE:
        headers["x-csrf-token"] = csrf
    return headers


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE == "postgres":
        skip = pytest.mark.skip(reason="SQLite-specific test (suite running on PostgreSQL)")
        for item in items:
            if "sqlite_only" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def postgres_factory():
    """Databases on a PostgreSQL server: GOVERNANCE_TEST_POSTGRES_URL, else a throwaway local cluster."""
    url = admin_url_from_environment()
    server = None
    if url is None:
        server = TemporaryPostgres()
        if not server.available:
            pytest.skip("PostgreSQL is not available (set GOVERNANCE_TEST_POSTGRES_URL or install PostgreSQL)")
        url = server.start()
    factory = DatabaseFactory(url)
    yield factory
    factory.close()
    if server is not None:
        server.stop()


@pytest.fixture
def postgres_url(postgres_factory):
    """An empty PostgreSQL database for this test (dropped afterwards)."""
    name = postgres_factory.create()
    yield postgres_factory.url_for(name)
    postgres_factory.drop(name)


@pytest.fixture
def postgres_migrated_url(postgres_factory):
    """A PostgreSQL database already at the head revision (copied from a template)."""
    name = postgres_factory.create(template=postgres_factory.migrated_template())
    yield postgres_factory.url_for(name)
    postgres_factory.drop(name)


@pytest.fixture
def settings(tmp_path: Path, request) -> Settings:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><div id=root></div>")
    (frontend / "app.js").write_text("console.log('app')")
    (frontend / "style.css").write_text("body{}")
    database = {"governance_database_url": request.getfixturevalue("postgres_migrated_url")} if TEST_DATABASE == "postgres" else {}
    return Settings(
        **database,
        governance_environment="test",
        governance_data_dir=tmp_path / "data",
        governance_frontend_dir=frontend,
        governance_allowed_hosts="testserver",
        governance_reminders_enabled=False,
        openai_api_key="", openai_model="", resend_api_key="", mail_from="",
        _env_file=None,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings), base_url="http://testserver") as test_client:
        yield test_client


class Api:
    """Drives the API like the React app, holding the session cookie of the current profile."""

    def __init__(self, client: TestClient, cookie_name: str = COOKIE):
        self.client = client
        self.cookie: str | None = None
        self.cookie_name = cookie_name

    # Sessions ------------------------------------------------------------------------------
    def sign_in(self) -> "Api":
        self.client.cookies.clear()
        response = self.client.get("/")
        assert response.status_code == 200
        self.cookie = response.cookies.get(COOKIE)
        assert self.cookie
        assert response.cookies.get(CSRF_COOKIE) == csrf_for(self.cookie)
        return self

    def role(self, actor: str) -> None:
        response = self.request("POST", "/api/session", json={"actor": actor})
        assert response.status_code == 200, response.text
        self.cookie = response.cookies.get(COOKIE)

    def request(self, method: str, path: str, **kwargs):
        self.client.cookies.clear()
        headers = {**browser_headers(method, self.cookie, self.cookie_name), **(kwargs.pop("headers", {}) or {})}
        response = self.client.request(method, path, headers=headers, **kwargs)
        self.client.cookies.clear()
        return response

    # JSON helpers -------------------------------------------------------------------------
    def call(self, path: str, body: dict | None = None, expect: int = 200) -> dict:
        response = self.request("POST" if body is not None else "GET", path, json=body)
        assert response.status_code == expect, f"{response.status_code} {response.text}"
        return response.json()

    def v2(self, body: dict | None = None, expect: int = 200) -> dict:
        return self.call("/api/v2", body, expect)

    def portal(self, body: dict | None = None, expect: int = 200) -> dict:
        return self.call("/api/portal", body, expect)

    def state(self) -> dict:
        return self.v2()

    def get(self, record_id: str) -> dict | None:
        return next((r for r in self.state()["records"] if r["id"] == record_id), None)

    def create(self, kind: str, data: dict, expect: int = 200) -> str | None:
        return self.v2({"action": "create", "kind": kind, "data": data}, expect).get("id")

    def action(self, record_id: str, action: str, body: dict | None = None, expect: int = 200) -> dict:
        record = self.get(record_id)
        assert record, f"missing {record_id}"
        return self.v2({"id": record_id, "revision": record["revision"], "action": action, **(body or {})}, expect)

    def edit(self, record_id: str, data: dict, expect: int = 200) -> dict:
        return self.action(record_id, "edit", {"data": data}, expect)

    def move(self, record_id: str, to: str, expect: int = 200) -> dict:
        return self.action(record_id, "transition", {"status": to}, expect)

    def upload(self, company: str, previous: str = "", content: bytes = b"Evidence text", filename: str = "evidence.txt",
               expect: int = 200, **fields) -> str | None:
        data = {"company": company, "previous": previous, **{k: str(v) for k, v in fields.items()}}
        response = self.request("POST", "/api/v2-files", data=data, files={"file": (filename, content)})
        assert response.status_code == expect, response.text
        return response.json().get("id")

    def trial_balance(self, company: str, content: str = "account,debit,credit\nCash,100,0\nCapital,0,100",
                      expect: int = 200, period: str = "2026-09") -> str | None:
        response = self.request("POST", "/api/v2-import", data={"company": company, "period": period, "currency": "INR"},
                                files={"file": ("trialbalance.csv", content.encode())})
        assert response.status_code == expect, response.text
        return response.json().get("id")


@pytest.fixture
def api(client: TestClient) -> Api:
    """Signed-in owner with the local demo profiles initialised (as the React app does on first load)."""
    session = Api(client).sign_in()
    session.v2({"action": "initialise"})
    return session
