"""Phase 4: PostgreSQL schema, constraints, multi-process concurrency and the SQLite → PostgreSQL transfer.

Runs against a real PostgreSQL (GOVERNANCE_TEST_POSTGRES_URL or a throwaway local cluster); skipped
when none is available. Concurrency tests use separate OS processes, each with its own engine and
connection pool, released together by a barrier.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import shutil
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import DEFAULT_DATA_DIR, Settings
from app.core.errors import Conflict
from app.database.migrations import current_revision, head_revision, upgrade_to_head
from app.database.transfer import transfer
from app.main import create_app
from app.repositories.records import RecordRepository
from app.services.identity_service import create_organization, set_credentials
from tests import concurrency_workers as workers
from tests.conftest import Api

pytestmark = pytest.mark.postgres
TODAY = date.today().isoformat()
REAL_DB = DEFAULT_DATA_DIR / "preview.sqlite"


def make_settings(tmp_path: Path, url: str | None = None, **extra) -> Settings:
    frontend = tmp_path / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "index.html").write_text("<!doctype html><div id=root></div>")
    values = dict(governance_environment="test", governance_data_dir=tmp_path / "data", governance_frontend_dir=frontend,
                  governance_allowed_hosts="testserver", governance_reminders_enabled=False, _env_file=None)
    if url:
        values["governance_database_url"] = url
    values.update(extra)
    return Settings(**values)


@pytest.fixture
def pg(tmp_path, postgres_migrated_url):
    settings = make_settings(tmp_path, postgres_migrated_url)
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        api = Api(client).sign_in()
        api.v2({"action": "initialise"})
        yield {"api": api, "client": client, "services": client.app.state.services, "url": postgres_migrated_url,
               "data_dir": str(settings.governance_data_dir)}


def run_processes(function, argument_lists: list[tuple]) -> list:
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(len(argument_lists))
        with context.Pool(len(argument_lists)) as pool:
            # Every worker takes (url, data_dir, barrier, ...): the barrier releases them together.
            return pool.starmap(function, [(*args[:2], barrier, *args[2:]) for args in argument_lists])


# Schema --------------------------------------------------------------------------------------

def test_schema_uses_postgresql_types_and_constraints(pg):
    engine = pg["services"].db.engine
    inspector = sa.inspect(engine)
    types = {c["name"]: str(c["type"]) for c in inspector.get_columns("records")}
    assert types["data"].startswith("JSONB") and types["seq"] == "BIGINT"
    assert current_revision(engine) == head_revision()
    with engine.connect() as connection:
        timeout = connection.exec_driver_sql("SHOW statement_timeout").scalar()
        assert timeout == "30s"  # GOVERNANCE_DB_STATEMENT_TIMEOUT_MS default
    def attempt(sql: str, **params):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(sa.text(sql), params)
    # Data must be a JSON object; owners must exist; owners cannot change; company access needs a company.
    attempt("INSERT INTO records (owner, id, kind, data, updated) VALUES ('local-preview', 'x', 'task', '[1]', 'now')")
    attempt("INSERT INTO records (owner, id, kind, data, updated) VALUES ('nobody', 'x', 'task', '{}', 'now')")
    attempt("UPDATE records SET owner = 'other' WHERE owner = 'local-preview'")
    attempt("INSERT INTO company_access (organization_id, membership_id, company_id, created_at) "
            "SELECT organization_id, id, 'secretary', 'now' FROM organization_memberships WHERE member_id = 'secretary'")


def test_expression_index_matches_the_queries(pg):
    with pg["services"].db.engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in connection.execute(sa.text(
            "EXPLAIN SELECT id FROM records WHERE owner = 'local-preview' AND (data ->> 'company') = CAST(:c AS VARCHAR) AND kind = 'task'"),
            {"c": "c1"}))
    assert "ix_records_owner_company_kind" in plan


# Multi-process concurrency --------------------------------------------------------------------

def _billing_setup(api: Api, invoices: int, number: str | None = "INV-RACE") -> dict:
    company = api.create("company", {"name": "Race Co"})
    api.portal({"action": "companySave", "id": company, "revision": api.get(company)["revision"], "data": {"name": "Race Co", "address": "1 Road"}})
    data = {"number": number, "due": TODAY, "items": [{"description": "Fee", "quantity": 1, "rate": "100"}], "seller": {"name": "Firm", "address": "Street"}}
    ids = [api.portal({"action": "invoiceCreate", "company": company, "data": data if number else {**data, "number": f"INV-{i}"}})["id"]
           for i in range(invoices)]
    return {"company": company, "invoices": ids}


def test_invoice_numbers_stay_unique_across_processes(pg):
    setup = _billing_setup(pg["api"], 6)
    revisions = {i["id"]: i["revision"] for i in pg["api"].portal()["invoices"]}
    statuses = run_processes(workers.issue_invoice, [(pg["url"], pg["data_dir"], setup["company"], i, revisions[i]) for i in setup["invoices"]])
    assert statuses.count(200) == 1 and set(statuses) <= {200, 409}, statuses
    issued = [i for i in pg["api"].portal()["invoices"] if i["status"] == "Issued"]
    assert len(issued) == 1


def test_payment_references_stay_unique_across_processes(pg):
    api = pg["api"]
    setup = _billing_setup(api, 2, number=None)
    for invoice in setup["invoices"]:
        api.portal({"action": "invoiceIssue", "company": setup["company"], "id": invoice, "revision": api.get(invoice)["revision"]})
    proof = api.upload(setup["company"], shared="true")
    revisions = {i: api.get(i)["revision"] for i in setup["invoices"]}
    arguments = [(pg["url"], pg["data_dir"], setup["company"], invoice, revisions[invoice], proof, "UTR-RACE-1")
                 for invoice in setup["invoices"] for _ in range(3)]
    statuses = run_processes(workers.report_payment, arguments)
    assert statuses.count(200) == 1 and set(statuses) <= {200, 400, 409}, statuses
    assert len([p for p in api.portal()["payments"] if p["reference"].lower() == "utr-race-1"]) == 1


def test_optimistic_locking_holds_across_processes(pg):
    api = pg["api"]
    company = api.create("company", {"name": "Edit Co"})
    task = api.create("task", {"company": company, "title": "Original", "due": TODAY})
    revision = api.get(task)["revision"]
    statuses = run_processes(workers.edit_task, [(pg["url"], pg["data_dir"], task, revision, n) for n in range(6)])
    assert statuses.count(200) == 1 and statuses.count(409) == 5, statuses
    assert api.get(task)["revision"] == revision + 1


def test_only_one_process_holds_a_lease(pg):
    results = run_processes(workers.take_lease, [(pg["url"], pg["data_dir"], f"process-{n}") for n in range(6)])
    assert results.count(True) == 1


def test_concurrent_migrations_are_serialised(postgres_url):
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(4)
        with context.Pool(4) as pool:
            revisions = pool.starmap(workers.migrate, [(postgres_url, barrier)] * 4)
    assert revisions == [head_revision()] * 4


def test_database_uniqueness_backs_up_the_application_checks(pg):
    """Even code that skipped the application check cannot issue a duplicate number."""
    api, services = pg["api"], pg["services"]
    setup = _billing_setup(api, 2)
    first, second = setup["invoices"]
    api.portal({"action": "invoiceIssue", "company": setup["company"], "id": first, "revision": api.get(first)["revision"]})
    record = api.get(second)
    with pytest.raises(Conflict, match="Invoice number already issued"):
        with services.db.write() as session:
            RecordRepository(session, "local-preview").update_if_revision(second, record["revision"], {**record, "status": "Issued"})


# SQLite → PostgreSQL transfer -----------------------------------------------------------------

def build_sqlite_workspace(tmp_path: Path) -> tuple[Settings, dict]:
    """A SQLite workspace with every kind of record, files, audit history and legacy-style text."""
    settings = make_settings(tmp_path / "sqlite-side")
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        api = Api(client).sign_in()
        api.v2({"action": "initialise"})
        c1 = api.create("company", {"name": "Résumé & Sons — \"Quoted\""})
        c2 = api.create("company", {"name": "Second Co"})
        doc = api.upload(c1, content=b"v1")
        api.upload(c1, previous=doc, content=b"v2")
        shared = api.upload(c2, content=b"shared", shared="true")
        task = api.create("task", {"company": c1, "title": "Task \\ with backslash", "due": TODAY, "assignee": "secretary"})
        api.create("event", {"company": c1, "title": "Appoint", "due": TODAY, "eventType": "Director appointment", "effective": TODAY})
        api.create("meeting", {"company": c2, "title": "Board", "due": TODAY, "agenda": "A"})
        client_id = api.v2({"action": "member", "name": "Client person", "role": "Client", "companies": [c2]})["id"]
        api.create("request", {"company": c2, "title": "Docs please", "due": TODAY, "assignee": client_id})
        api.edit(task, {"title": "Task edited"})
        api.v2({"action": "automation"})
        api.v2({"action": "briefing"})
        api.portal({"action": "companySave", "id": c2, "revision": api.get(c2)["revision"], "data": {"name": "Second Co", "address": "Road"}})
        invoice = api.portal({"action": "invoiceCreate", "company": c2, "data": {"number": "N1", "due": TODAY, "items": [
            {"description": "Fee", "quantity": 1, "rate": "100"}], "seller": {"name": "Firm", "address": "Street"}}})["id"]
        api.portal({"action": "invoiceIssue", "company": c2, "id": invoice, "revision": api.get(invoice)["revision"]})
        api.portal({"action": "paymentReport", "company": c2, "id": invoice, "revision": api.get(invoice)["revision"], "amount": "40",
                    "reference": "UTR-1", "paidAt": TODAY, "evidence": [shared]})
        state = api.state()
        portal = api.portal()
    return settings, {"state": state, "portal": portal, "c1": c1, "doc": doc}


def _canonical(state: dict) -> dict:
    """Same content regardless of JSON key order (JSONB orders keys differently from SQLite text)."""
    return json.loads(json.dumps(state, sort_keys=True))


def test_transfer_copies_and_reconciles_a_workspace(tmp_path, postgres_url):
    settings, before = build_sqlite_workspace(tmp_path)
    source = settings.database_path
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report = transfer(source, postgres_url)
    assert report.ok, report.problems
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest  # the source is never modified
    assert report.tables["records"]["source"] == report.tables["records"]["identical"] > 20
    assert report.tables["audit"]["identical"] > 10
    # The application on PostgreSQL shows exactly the same workspace, files included.
    pg_settings = settings.model_copy(update={"governance_database_url": settings.governance_database_url.__class__(postgres_url)})
    with TestClient(create_app(pg_settings), base_url="http://testserver") as client:
        api = Api(client).sign_in()
        after_state, after_portal = api.state(), api.portal()
        file = api.request("GET", f"/api/v2-files?id={before['doc']}")
    for key in ("records", "members", "audit"):
        assert _canonical(after_state[key]) == _canonical(before["state"][key]), key
    assert [r["id"] for r in after_state["records"]] == [r["id"] for r in before["state"]["records"]]  # same order
    assert _canonical(after_portal) == _canonical(before["portal"])
    assert file.status_code == 200 and file.content == b"v1"


def test_transfer_refuses_a_non_empty_target(tmp_path, postgres_migrated_url):
    settings, _ = build_sqlite_workspace(tmp_path)
    assert transfer(settings.database_path, postgres_migrated_url).ok
    second = transfer(settings.database_path, postgres_migrated_url)
    assert not second.ok and "already contains data" in " ".join(second.problems)


def test_transfer_stops_before_writing_invalid_documents(tmp_path, postgres_url):
    settings, _ = build_sqlite_workspace(tmp_path)
    import sqlite3
    with sqlite3.connect(settings.database_path) as db:
        db.execute("INSERT INTO records (owner, id, kind, data, revision, updated) VALUES ('local-preview', 'legacy', 'task', '[1,2]', 1, 'x')")
    db.close()
    report = transfer(settings.database_path, postgres_url)
    assert not report.ok and any("not a JSON object" in p for p in report.problems)
    engine = sa.create_engine(postgres_url, poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM records")).scalar() == 0  # nothing written
    engine.dispose()


@pytest.mark.skipif(not REAL_DB.is_file(), reason="no local workspace database")
def test_transfer_copies_boolean_columns_correctly(tmp_path, postgres_url):
    """Regression test: SQLite stores booleans as plain integers (0/1); PostgreSQL's driver
    refuses to bind an int for a boolean column, so ``users.must_change_password`` must be
    coerced before the row is sent (found transferring a real workspace with password accounts).

    One row holds the SQLite default (0 -> False); one is forced to True directly (the column's
    CLI-only bootstrap path, ``set_credentials`` with ``must_change_password=True``), so both
    boolean values are exercised regardless of what the member-creation UI currently does.
    """
    settings = make_settings(tmp_path / "bool-side", governance_auth_mode="password", governance_mail_provider="disabled",
                             governance_public_url="https://testserver")
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        services = client.app.state.services
        with services.db.write() as session:
            _org, owner = create_organization(session, "firm-bool", "Firm Bool", "Bo Owner")
            set_credentials(session, owner.id, "owner@firm-bool.test", "a firm bool pass phrase")  # False (default)
            from app.repositories.auth import AuthRepository
            from app.auth.passwords import hash_password
            from app.services.identity_service import add_member

            pending = add_member(session, "firm-bool", "pending", "Pending Person", "Accountant", [])
            AuthRepository(session).set_password(pending.user_id, hash_password("some other pass phrase"), must_change_password=True)

    report = transfer(settings.database_path, postgres_url)
    assert report.ok, report.problems
    engine = sa.create_engine(postgres_url, poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        rows = {r[0]: r[1] for r in connection.execute(sa.text(
            "SELECT m.member_id, u.must_change_password FROM users u JOIN organization_memberships m ON m.user_id = u.id"))}
    engine.dispose()
    assert rows["owner"] is False, rows
    assert rows["pending"] is True, rows


def test_a_copy_of_the_real_workspace_transfers_exactly(tmp_path, postgres_url):
    import sqlite3
    copy = tmp_path / "real-copy.sqlite"
    with sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True) as reader, sqlite3.connect(copy) as writer:
        reader.backup(writer)
    reader.close()
    writer.close()
    report = transfer(copy, postgres_url)
    assert report.ok, report.problems
    assert report.tables["records"]["source"] == report.tables["records"]["identical"]


def test_identical_trial_balance_imports_stay_unique_across_processes(pg):
    company = pg["api"].create("company", {"name": "Ledger Co"})
    content = b"account,debit,credit\nCash,100,0\nCapital,0,100"
    files_before = set(Path(pg["data_dir"], "files").iterdir())
    statuses = run_processes(workers.import_trial_balance, [(pg["url"], pg["data_dir"], company, content)] * 4)
    assert statuses.count(200) == 1 and set(statuses) <= {200, 400, 409}, statuses
    assert len([r for r in pg["api"].state()["records"] if r["kind"] == "trialbalance"]) == 1
    assert len(set(Path(pg["data_dir"], "files").iterdir()) - files_before) == 1  # losers removed their file


def test_two_owners_cannot_deactivate_each_other_at_once(pg):
    from app.services.identity_service import add_member

    services = pg["services"]
    with services.db.write() as session:
        RecordRepository(session, "local-preview").insert("owner2", "member", {"name": "Second owner", "role": "Owner", "companies": ["*"], "active": True})
        add_member(session, "local-preview", "owner2", "Second owner", "Owner", ["*"])
    statuses = run_processes(workers.deactivate_member, [(pg["url"], pg["data_dir"], "owner", "owner2"),
                                                         (pg["url"], pg["data_dir"], "owner2", "owner")])
    assert sorted(statuses) in ([200, 400], [200, 403]), statuses
    with services.db.read() as session:
        active = session.execute(sa.text("SELECT COUNT(*) FROM organization_memberships WHERE organization_id = 'local-preview' "
                                         "AND role = 'Owner' AND status = 'active'")).scalar()
    assert active == 1


def test_bulk_review_flags_never_overwrite_a_concurrent_edit(pg):
    """A new document version flags dependent work (``patch_where``) while someone edits that work."""
    api = pg["api"]
    company = api.create("company", {"name": "Evidence Co"})
    for round_ in range(4):
        document = api.upload(company, content=f"round {round_} v1".encode())
        task = api.create("task", {"company": company, "title": "Original", "due": TODAY, "evidence": [document]})
        revision = api.get(task)["revision"]
        upload, edit = run_processes_mixed([
            (workers.upload_new_version, (pg["url"], pg["data_dir"], company, document)),
            (workers.edit_title, (pg["url"], pg["data_dir"], task, revision, f"Edited {round_}")),
        ])
        final = api.get(task)
        assert upload == 200 and final["reviewRequired"] is True
        assert edit in (200, 400, 409)
        if edit == 200:  # the edit was accepted, so it must not have been lost
            assert final["title"] == f"Edited {round_}" and final["revision"] == revision + 2
        else:
            assert final["title"] == "Original" and final["revision"] == revision + 1


def run_processes_mixed(calls: list[tuple]) -> list:
    """Different worker functions released together by one barrier."""
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(len(calls))
        with context.Pool(len(calls)) as pool:
            pending = [pool.apply_async(function, (*args[:2], barrier, *args[2:])) for function, args in calls]
            return [result.get(timeout=120) for result in pending]


def test_worker_processes_can_start_together_on_an_empty_database(tmp_path, postgres_url):
    data_dir = str(tmp_path / "data")
    results = run_processes(workers.start_application, [(postgres_url, data_dir)] * 4)
    assert results == ["started"] * 4
    engine = sa.create_engine(postgres_url, poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        assert current_revision(engine) == head_revision()
        assert connection.execute(sa.text("SELECT COUNT(*) FROM organizations")).scalar() == 1
    engine.dispose()


def test_transfer_reports_nul_text_in_any_table_before_writing(tmp_path, postgres_url):
    settings, _ = build_sqlite_workspace(tmp_path)
    import sqlite3
    with sqlite3.connect(settings.database_path) as db:  # legacy data written before input was cleaned
        db.execute("UPDATE audit SET subject = 'bad' || char(0) || 'subject' WHERE rowid = (SELECT MIN(rowid) FROM audit)")
    db.close()
    report = transfer(settings.database_path, postgres_url)
    assert not report.ok and any(p.startswith("audit.subject: 1 row(s) contain a NUL") for p in report.problems)
    engine = sa.create_engine(postgres_url, poolclass=sa.pool.NullPool)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM audit")).scalar() == 0
    engine.dispose()
