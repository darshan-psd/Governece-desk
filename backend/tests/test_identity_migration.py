"""Migration 0002 (identity and tenancy): adoption of existing data, idempotency, failure safety."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from app.core.config import BACKEND_DIR, DEFAULT_DATA_DIR
from app.database import migrations
from app.database.migrations import current_revision, upgrade_to_head
from app.main import create_app
from app.services.identity_service import ensure_preview_organization, identity_ids
from tests.conftest import COOKIE, Api
from tests.test_foundation import ORIGINAL_COLUMNS

pytestmark = pytest.mark.sqlite_only  # SQLite files, adoption and query plans

REAL_DB = DEFAULT_DATA_DIR / "preview.sqlite"
IDENTITY_TABLES = {"organizations", "users", "organization_memberships", "company_access", "background_leases"}
T = "2026-01-01T00:00:00.000Z"


def snapshot(path: Path) -> dict:
    conn = sqlite3.connect(path)
    out = {table: conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1, 2").fetchall() for table, columns in ORIGINAL_COLUMNS.items()}
    conn.close()
    return out


def tables(path: Path) -> set:
    conn = sqlite3.connect(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
    conn.close()
    return names


def query(path: Path, sql: str, params=()) -> list:
    conn = sqlite3.connect(path)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def legacy_workspace(path: Path, *, extra_profiles: list[tuple] = (), second_tenant: bool = False) -> Path:
    """A database at revision 0001 shaped like a real pre-Phase-2 workspace."""
    engine = create_engine(f"sqlite:///{path}")
    upgrade_to_head(engine, target="0001_baseline")
    engine.dispose()
    conn = sqlite3.connect(path)
    rows = [
        ("local-preview", "co-1", "company", {"name": "Acme Pvt Ltd"}),
        ("local-preview", "co-2", "company", {"name": "Beta LLP"}),
        ("local-preview", "doc-1", "document", {"name": "minutes.txt", "company": "co-1", "key": "local-preview/doc-1", "shared": True}),
        ("local-preview", "owner", "member", {"name": "Workspace owner", "role": "Owner", "companies": ["*"], "active": True}),
        ("local-preview", "secretary", "member", {"name": "Priya Sharma", "role": "Secretary", "companies": ["*"], "active": True}),
        ("local-preview", "acct", "member", {"name": "Scoped accountant", "role": "Accountant", "companies": ["co-2"], "active": True}),
        ("local-preview", "client", "member", {"name": "Northstar client", "role": "Client", "companies": ["co-1", "c1-missing"], "active": True}),
        ("local-preview", "gone", "member", {"name": "Former staff", "role": "Auditor", "companies": ["*"], "active": False}),
        *extra_profiles,
    ]
    if second_tenant:
        rows += [("tenant-two", "co-9", "company", {"name": "Other firm client"}),
                 ("tenant-two", "boss", "member", {"name": "Other owner", "role": "Owner", "companies": ["*"], "active": True})]
    for owner, record_id, kind, data in rows:
        conn.execute("INSERT INTO records VALUES (?,?,?,?,1,?)", (owner, record_id, kind, json.dumps(data), T))
    conn.execute("INSERT INTO audit VALUES ('a-1','local-preview','Created company · Workspace owner','Acme Pvt Ltd',?)", (T,))
    conn.execute("INSERT INTO sessions VALUES ('hash-secretary','local-preview','secretary',?, '2999-01-01T00:00:00.000Z')", (T,))
    conn.execute("INSERT INTO sessions VALUES ('hash-unknown','local-preview','nobody',?, '2999-01-01T00:00:00.000Z')", (T,))
    conn.commit()
    conn.close()
    return path


def test_fresh_database_gets_the_identity_schema_and_a_preview_organization(tmp_path, settings):
    path = tmp_path / "fresh.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    upgrade_to_head(engine)
    assert current_revision(engine) == migrations.head_revision()
    engine.dispose()
    assert IDENTITY_TABLES <= tables(path)
    assert query(path, "SELECT count(*) FROM organizations") == [(0,)]  # no data, no organizations
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        ensure_preview_organization(client.app.state.services)  # idempotent second call
        path = settings.database_path
        assert query(path, "SELECT id, status FROM organizations") == [("local-preview", "active")]
        assert query(path, "SELECT member_id, role, all_companies FROM organization_memberships") == [("owner", "Owner", 1)]


def test_existing_workspace_is_adopted_without_changing_data(tmp_path):
    path = legacy_workspace(tmp_path / "legacy.sqlite")
    before = snapshot(path)
    engine = create_engine(f"sqlite:///{path}")
    upgrade_to_head(engine)
    assert current_revision(engine) == migrations.head_revision()
    engine.dispose()
    assert snapshot(path) == before  # records (with rowids), audit and sessions: identical
    assert query(path, "SELECT id, name, status FROM organizations") == [("local-preview", "Local preview workspace", "active")]
    memberships = {row[0]: row[1:] for row in query(path, "SELECT member_id, role, status, all_companies FROM organization_memberships")}
    assert memberships == {"owner": ("Owner", "active", 1), "secretary": ("Secretary", "active", 1), "acct": ("Accountant", "active", 0),
                           "client": ("Client", "active", 0), "gone": ("Auditor", "inactive", 1)}
    access = {(m, c) for m, c in query(path, "SELECT m.member_id, a.company_id FROM company_access a JOIN organization_memberships m ON m.id = a.membership_id")}
    assert access == {("acct", "co-2"), ("client", "co-1")}  # 'c1-missing' does not exist and grants nothing
    assert query(path, "SELECT workspace, actor, membership_id IS NOT NULL FROM sessions ORDER BY actor") == [
        ("local-preview", "nobody", 0), ("local-preview", "secretary", 1)]
    assert query(path, "SELECT actor_membership_id FROM audit") == [(None,)]  # no invented actor for history
    assert json.loads(query(path, "SELECT data FROM records WHERE id = 'client'")[0][0])["companies"] == ["co-1", "c1-missing"]


def test_adopted_workspace_works_end_to_end_with_its_files(tmp_path, settings):
    data = settings.governance_data_dir
    data.mkdir(parents=True)
    legacy_workspace(data / "preview.sqlite")
    (data / "files").mkdir()
    (data / "files" / "local-preview%2Fdoc-1").write_bytes(b"minutes written before Phase 2")
    with TestClient(create_app(settings), base_url="http://testserver") as client:  # migrates on start (test profile)
        api = Api(client).sign_in()
        assert api.request("GET", "/api/v2-files?id=doc-1").content == b"minutes written before Phase 2"
        assert {m["id"] for m in api.state()["members"]} == {"owner", "secretary", "acct", "client", "gone"}
        api.role("client")
        assert {c["id"] for c in api.portal()["companies"]} == {"co-1"}
        assert api.request("GET", "/api/v2-files?id=doc-1").status_code == 200  # shared document of its company
        api.role("acct")
        assert {r["id"] for r in api.state()["records"] if r["kind"] == "company"} == {"co-2"}
        assert api.request("POST", "/api/session", json={"actor": "gone"}).status_code == 403  # inactive profile


def test_legacy_sessions_are_linked_or_rejected(tmp_path, settings):
    data = settings.governance_data_dir
    data.mkdir(parents=True)
    path = legacy_workspace(data / "preview.sqlite")
    conn = sqlite3.connect(path)
    import hashlib
    conn.execute("UPDATE sessions SET token_hash = ? WHERE actor = 'secretary'", (hashlib.sha256(b"old-secretary-token").hexdigest(),))
    conn.execute("UPDATE sessions SET token_hash = ? WHERE actor = 'nobody'", (hashlib.sha256(b"old-unknown-token").hexdigest(),))
    conn.commit()
    conn.close()
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        client.cookies.clear()
        linked = client.get("/api/v2", headers={"cookie": f"{COOKIE}=old-secretary-token"})
        assert linked.status_code == 200 and linked.json()["actor"]["id"] == "secretary"
        assert client.get("/api/v2", headers={"cookie": f"{COOKIE}=old-unknown-token"}).status_code == 401


def test_each_legacy_tenant_becomes_its_own_isolated_organization(tmp_path, settings):
    data = settings.governance_data_dir
    data.mkdir(parents=True)
    legacy_workspace(data / "preview.sqlite", second_tenant=True)
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        assert {r[0] for r in query(settings.database_path, "SELECT id FROM organizations")} == {"local-preview", "tenant-two"}
        api = Api(client).sign_in()
        state = api.state()
        assert "co-9" not in {r["id"] for r in state["records"]} and "boss" not in {m["id"] for m in state["members"]}
        assert api.request("POST", "/api/session", json={"actor": "boss"}).status_code == 403


def test_adoption_is_idempotent(tmp_path):
    path = legacy_workspace(tmp_path / "legacy.sqlite")
    engine = create_engine(f"sqlite:///{path}")
    upgrade_to_head(engine)
    upgrade_to_head(engine)  # already at head: no-op
    engine.dispose()
    first = {t: query(path, f"SELECT * FROM {t} ORDER BY 1") for t in ("organizations", "users", "organization_memberships", "company_access")}
    module = _migration_module()
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        report = module.adopt_existing_data(conn)  # running the adoption step again inserts nothing
    engine.dispose()
    assert report["organizations"] == report["memberships"] == report["company_access"] == report["sessions_linked"] == 0
    assert {t: query(path, f"SELECT * FROM {t} ORDER BY 1") for t in first} == first
    assert identity_ids("local-preview", "secretary") == module.identity_ids("local-preview", "secretary")


def _migration_module():
    spec = importlib.util.spec_from_file_location("m0002", BACKEND_DIR / "migrations" / "versions" / "0002_identity_tenancy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_migration_interrupted_at_its_last_step_leaves_the_database_unchanged(tmp_path, monkeypatch):
    path = legacy_workspace(tmp_path / "legacy.sqlite")
    before, schema_before = snapshot(path), tables(path)
    real_engine = migrations.transactional_engine

    def crashing_engine(url):
        engine = real_engine(url)

        @event.listens_for(engine, "before_cursor_execute")
        def crash(conn, cursor, statement, *args):
            if statement.lstrip().startswith("CREATE TRIGGER trg_sessions_membership_consistent"):
                raise RuntimeError("simulated crash: disk full")

        return engine

    monkeypatch.setattr(migrations, "transactional_engine", crashing_engine)
    engine = create_engine(f"sqlite:///{path}")
    with pytest.raises(Exception, match="simulated crash"):
        upgrade_to_head(engine)
    assert current_revision(engine) == "0001_baseline"
    engine.dispose()
    assert tables(path) == schema_before and snapshot(path) == before  # DDL and data both rolled back


@pytest.mark.parametrize("profile, message", [
    (("local-preview", "weird", "member", {"name": "?", "role": "Superuser", "companies": ["*"]}), "unknown role"),
    (("local-preview", "wide", "member", {"name": "?", "role": "Client", "companies": ["*"]}), "every company"),
])
def test_invalid_existing_data_stops_the_migration_safely(tmp_path, profile, message):
    path = legacy_workspace(tmp_path / "legacy.sqlite", extra_profiles=[profile])
    before, schema_before = snapshot(path), tables(path)
    engine = create_engine(f"sqlite:///{path}")
    with pytest.raises(Exception, match=message):
        upgrade_to_head(engine)
    assert current_revision(engine) == "0001_baseline"
    engine.dispose()
    assert tables(path) == schema_before and snapshot(path) == before


def test_identity_revision_cannot_be_downgraded(tmp_path):
    from alembic import command

    from app.database.sqlite import transactional_engine

    engine = create_engine(f"sqlite:///{legacy_workspace(tmp_path / 'legacy.sqlite')}")
    upgrade_to_head(engine)
    config = migrations.alembic_config()
    migration_engine = transactional_engine(engine.url)
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        with migration_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0001_baseline")
    migration_engine.dispose()
    assert current_revision(engine) == migrations.head_revision()
    engine.dispose()


@pytest.mark.skipif(not REAL_DB.is_file(), reason="no local workspace database")
def test_a_copy_of_the_real_workspace_adopts_cleanly(tmp_path):
    """Read-only on the real file: copied with SQLite's backup API, then the copy is migrated."""
    copy = tmp_path / "copy.sqlite"
    source = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    source.backup(target)
    source.close()
    target.close()
    engine = create_engine(f"sqlite:///{copy}")
    if current_revision(engine) != "0001_baseline":
        engine.dispose()
        pytest.skip("the real workspace is not at the previous revision")
    before = snapshot(copy)
    upgrade_to_head(engine)
    engine.dispose()
    assert snapshot(copy) == before
    profiles = query(copy, "SELECT count(*) FROM records WHERE kind = 'member'")[0][0]
    memberships = query(copy, "SELECT count(*) FROM organization_memberships")[0][0]
    assert memberships == profiles + (0 if query(copy, "SELECT 1 FROM records WHERE kind='member' AND id='owner'") else 1)
    assert query(copy, "SELECT count(*) FROM sessions WHERE membership_id IS NULL AND actor IN "
                       "(SELECT member_id FROM organization_memberships)") == [(0,)]
