"""Phase 1 (engineering foundation): profiles, logging, request IDs, health, headers, docs, migrations."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import DEFAULT_DATA_DIR, PROJECT_DIR, ConfigurationError, Settings
from app.core.logging import redact
from app.database.migrations import current_revision, head_revision, upgrade_to_head
from app.main import create_app
from tests.conftest import COOKIE, Api

NODE_MIGRATION = PROJECT_DIR / "drizzle" / "0000_redundant_luke_cage.sql"
REAL_DB = DEFAULT_DATA_DIR / "preview.sqlite"


def deployed(tmp_path: Path, env: str = "production", **overrides) -> Settings:
    values = dict(governance_environment=env, governance_auth_mode="password", governance_data_dir=tmp_path / "data",
                  governance_allowed_hosts="testserver", governance_public_url="https://testserver", governance_mail_provider="disabled", governance_reminders_enabled=False, _env_file=None)
    values.update(overrides)
    return Settings(**values)


def migrated(settings: Settings) -> Settings:
    settings.governance_data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url)
    upgrade_to_head(engine)
    engine.dispose()
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    return settings


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


# Environment profiles -------------------------------------------------------------------------

def test_development_defaults_keep_the_local_workflow():
    settings = Settings(_env_file=None)
    assert settings.environment == "development" and settings.local_preview
    assert settings.governance_data_dir == DEFAULT_DATA_DIR
    assert settings.api_docs_enabled and settings.auto_migrate and not settings.hsts_enabled and not settings.secure_cookies
    assert settings.problems() == []


def test_test_profile_requires_an_explicit_data_dir():
    assert any("GOVERNANCE_DATA_DIR" in p for p in Settings(governance_environment="test", _env_file=None).problems())


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_profiles_reject_preview_auth_and_missing_config(env):
    settings = Settings(governance_environment=env, _env_file=None)
    problems = " ".join(settings.problems())
    assert "local-preview" in problems and "GOVERNANCE_DATA_DIR" in problems and "GOVERNANCE_ALLOWED_HOSTS" in problems
    with pytest.raises(ConfigurationError):
        create_app(settings)


def test_deployed_profile_rejects_data_inside_the_source_tree(tmp_path):
    settings = deployed(tmp_path, governance_data_dir=PROJECT_DIR / ".sites-runtime" / "prod")
    assert any("outside the source tree" in p for p in settings.problems())
    assert any("AUTO_MIGRATE" in p for p in deployed(tmp_path, governance_auto_migrate=True).problems())


def test_secrets_never_appear_in_reprs_or_errors(tmp_path):
    secret = "sk-test-" + uuid.uuid4().hex
    settings = Settings(governance_environment="production", openai_api_key=secret, resend_api_key=secret, _env_file=None)
    assert secret not in repr(settings) and secret not in str(settings.model_dump())
    with pytest.raises(ConfigurationError) as error:
        settings.validate_runtime()
    assert secret not in str(error.value)
    assert settings.openai_api_key.get_secret_value() == secret


def test_database_password_is_never_exposed(tmp_path):
    from app.database import Database

    secret = "pw-" + uuid.uuid4().hex
    url = f"postgresql://governance:{secret}@127.0.0.1:1/governance"  # nothing listens on port 1
    settings = Settings(governance_environment="production", governance_database_url=url, _env_file=None)
    assert settings.uses_postgres and settings.database_url.startswith("postgresql+psycopg://")
    assert secret not in repr(settings) and secret not in str(settings.model_dump()) and secret not in " ".join(settings.problems())
    db = Database(settings.database_url, settings)
    assert secret not in repr(db.engine) and secret not in repr(db.url)
    with pytest.raises(Exception) as error:
        db.ping()
    assert secret not in str(error.value) and secret not in repr(error.value)
    db.dispose()


def test_production_requires_postgresql(tmp_path):
    problems = " ".join(deployed(tmp_path).problems())
    assert "GOVERNANCE_DATABASE_URL must point to PostgreSQL in production" in problems


@pytest.mark.postgres
def test_production_app_starts_safely(tmp_path, postgres_url):
    with TestClient(create_app(migrated(deployed(tmp_path, governance_database_url=postgres_url))), base_url="http://testserver") as client:
        page = client.get("/")
        assert COOKIE not in page.cookies  # no local-preview sign-in
        assert client.get("/api/v2").status_code == 401
        assert page.headers["strict-transport-security"].startswith("max-age=")
        for path in ("/docs", "/redoc", "/openapi.json", "/api/docs", "/api/openapi.json"):
            assert client.get(path).status_code == 404, path


@pytest.mark.postgres
def test_deployed_profile_refuses_an_unmigrated_database(tmp_path, postgres_url):
    with pytest.raises(ConfigurationError, match="alembic upgrade head"):
        with TestClient(create_app(deployed(tmp_path, env="staging", governance_database_url=postgres_url))):
            pass


@pytest.mark.postgres
def test_api_docs_can_be_enabled_explicitly(tmp_path, client, postgres_url):
    assert client.get("/api/docs").status_code == 200 and client.get("/api/openapi.json").status_code == 200
    settings = migrated(deployed(tmp_path, governance_api_docs=True, governance_database_url=postgres_url))
    with TestClient(create_app(settings), base_url="http://testserver") as prod:
        assert prod.get("/api/openapi.json").status_code == 200


# Request IDs and structured logs ---------------------------------------------------------------

def test_every_response_carries_a_request_id(api):
    generated = api.request("GET", "/api/v2").headers["x-request-id"]
    assert len(generated) == 32 and int(generated, 16) >= 0
    assert api.request("GET", "/api/v2", headers={"x-request-id": "trace-123"}).headers["x-request-id"] == "trace-123"
    replaced = api.request("GET", "/api/v2", headers={"x-request-id": "bad id\nwith spaces"}).headers["x-request-id"]
    assert replaced != "bad id\nwith spaces" and len(replaced) == 32
    assert api.request("GET", "/nope").headers["x-request-id"]
    assert api.client.get("/", headers={"host": "evil.example"}).headers["x-request-id"]  # rejected requests too


def test_request_log_is_structured_and_free_of_secrets(api, capsys):
    capsys.readouterr()
    token = api.cookie
    api.request("GET", "/api/v2-files?id=secret-query-value", headers={"authorization": "Bearer abc.def", "x-request-id": "log-check"})
    lines = json_lines(capsys.readouterr().out)
    entry = next(e for e in lines if e.get("event") == "http_request" and e.get("request_id") == "log-check")
    assert entry["method"] == "GET" and entry["path"] == "/api/v2-files" and entry["status"] == 404
    assert isinstance(entry["duration_ms"], float) and entry["level"] == "INFO" and entry["ts"].endswith("Z")
    output = json.dumps(lines)
    assert token not in output and "abc.def" not in output and "secret-query-value" not in output


def test_unhandled_errors_keep_the_generic_body_and_are_logged_with_the_request_id(client, capsys):
    def boom():
        raise RuntimeError("internal detail")
    client.app.add_api_route("/boom", boom)
    capsys.readouterr()
    response = client.get("/boom", headers={"x-request-id": "boom-1"})
    assert response.status_code == 500 and response.json() == {"error": "Unable to complete the local request."}
    assert "internal detail" not in response.text and response.headers["x-request-id"] == "boom-1"
    assert response.headers["x-content-type-options"] == "nosniff"
    error = next(e for e in json_lines(capsys.readouterr().out) if e.get("event") == "unhandled_error")
    assert error["request_id"] == "boom-1" and "RuntimeError" in error["exception"]


def test_redaction_masks_tokens_if_they_ever_reach_a_message():
    text = redact("cookie gov_preview=abc123; Authorization: Bearer xyz.789 password=hunter2 api_key: k-1")
    assert "abc123" not in text and "xyz.789" not in text and "hunter2" not in text and "k-1" not in text


# Health and readiness ---------------------------------------------------------------------------

def test_health_and_ready(client):
    for host in ("testserver", "10.0.0.7:8080"):  # load-balancer probes use their own Host header
        health = client.get("/health", headers={"host": host})
        assert health.status_code == 200 and health.json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {"database": "ok", "storage": "ok"}}


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a normal user")
def test_ready_reports_unavailable_storage_without_details(client, settings):
    files = settings.files_dir
    files.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        response = client.get("/ready")
    finally:
        files.chmod(stat.S_IRWXU)
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "ok", "storage": "unavailable"}}
    assert str(settings.governance_data_dir) not in response.text


# Security headers -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/app.js", "/api/v2", "/health", "/does-not-exist"])
def test_security_headers_on_every_response(api, path):
    headers = api.request("GET", path).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["x-frame-options"] == "DENY"
    csp = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self';" in csp and "object-src 'none'" in csp
    assert "strict-transport-security" not in headers  # development/test are plain HTTP
    assert len(headers.get_list("x-content-type-options")) == 1


# Data directory -----------------------------------------------------------------------------------

@pytest.mark.sqlite_only
def test_external_data_directory_is_used(tmp_path, settings):
    outside = tmp_path / "governance-data"
    settings = settings.model_copy(update={"governance_data_dir": outside})
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        api = Api(client).sign_in()
        company = api.create("company", {"name": "External"})
        api.upload(company)
    assert (outside / "preview.sqlite").is_file() and any((outside / "files").iterdir())


# Alembic --------------------------------------------------------------------------------------

# The columns that existed before any migration (later revisions only add nullable columns).
ORIGINAL_COLUMNS = {
    "records": "owner, id, kind, data, revision, updated, rowid",
    "audit": "id, owner, action, subject, at, rowid",
    "sessions": "token_hash, workspace, actor, created, expires",
}


def fingerprint(path: Path) -> dict:
    conn = sqlite3.connect(path)
    out = {}
    for table, columns in ORIGINAL_COLUMNS.items():
        rows = conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1, 2").fetchall()
        out[table] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    out["indexes"] = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
    conn.close()
    return out


EXPECTED_INDEXES = sorted([
    "audit_owner_at", "ix_records_owner_company_kind", "ix_records_owner_kind_updated", "records_owner_kind",  # 0001
    "ix_company_access_company", "ix_memberships_user", "ux_users_email",  # 0002
    "ux_sessions_public_id", "ix_sessions_user", "ix_sessions_membership", "ix_auth_tokens_user",  # 0003
    "ix_login_attempts_key", "ix_security_events_org_at", "ix_security_events_at",
    "ux_records_invoice_number", "ux_records_payment_reference", "ux_records_trialbalance_file",  # 0004
])


@pytest.mark.sqlite_only
def test_fresh_database_is_created_by_migrations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite'}")
    upgrade_to_head(engine)
    assert current_revision(engine) == head_revision() == "0005_forced_password_change"
    engine.dispose()
    assert fingerprint(tmp_path / "fresh.sqlite")["indexes"] == EXPECTED_INDEXES


@pytest.mark.sqlite_only
def test_node_era_database_is_adopted_without_touching_rows(tmp_path):
    path = tmp_path / "node.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(NODE_MIGRATION.read_text().replace("--> statement-breakpoint", ""))
    conn.execute("INSERT INTO records VALUES ('local-preview','c1','company','{\"name\":\"Node Co\"}',3,'2026-01-01T00:00:00.000Z')")
    conn.execute("INSERT INTO audit VALUES ('a1','local-preview','Created company · Owner','Node Co','2026-01-01T00:00:00.000Z')")
    conn.commit()
    conn.close()
    before = fingerprint_rows(path)
    engine = create_engine(f"sqlite:///{path}")
    upgrade_to_head(engine)
    upgrade_to_head(engine)  # idempotent
    assert current_revision(engine) == head_revision()
    engine.dispose()
    assert fingerprint_rows(path) == before
    assert fingerprint(path)["indexes"] == EXPECTED_INDEXES


def fingerprint_rows(path: Path) -> dict:
    conn = sqlite3.connect(path)
    out = {t: conn.execute(f"SELECT {ORIGINAL_COLUMNS[t]} FROM {t} ORDER BY 1, 2").fetchall() for t in ("records", "audit")}
    conn.close()
    return out


@pytest.mark.sqlite_only
@pytest.mark.skipif(not REAL_DB.is_file(), reason="no local workspace database")
def test_a_copy_of_the_real_workspace_database_migrates_with_identical_data(tmp_path):
    """Read-only on the real file: it is copied with SQLite's backup API, then the copy is migrated."""
    copy = tmp_path / "copy.sqlite"
    source = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    source.backup(target)
    source.close()
    target.close()
    before = fingerprint(copy)
    engine = create_engine(f"sqlite:///{copy}")
    upgrade_to_head(engine)
    assert current_revision(engine) == head_revision()
    engine.dispose()
    after = fingerprint(copy)
    assert {k: after[k] for k in ("records", "audit", "sessions")} == {k: before[k] for k in ("records", "audit", "sessions")}
    assert after["indexes"] == EXPECTED_INDEXES


@pytest.mark.sqlite_only
def test_incompatible_existing_table_is_refused(tmp_path):
    path = tmp_path / "odd.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (owner TEXT, id TEXT)")
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{path}")
    with pytest.raises(RuntimeError, match="refusing to adopt"):
        upgrade_to_head(engine)
    assert current_revision(engine) is None
    engine.dispose()


@pytest.mark.sqlite_only
def test_baseline_cannot_be_downgraded(tmp_path):
    from alembic import command

    from app.database.migrations import alembic_config

    from app.database.sqlite import transactional_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    upgrade_to_head(engine)
    config = alembic_config()
    # Same transactional connection the application and the alembic CLI use: the refused step
    # rolls back every step before it too.
    migration_engine = transactional_engine(engine.url)
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        with migration_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
    migration_engine.dispose()
    assert current_revision(engine) == head_revision()
    engine.dispose()
    assert fingerprint(tmp_path / "db.sqlite")["indexes"] == EXPECTED_INDEXES
