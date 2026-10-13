"""Phase 2: organizations, memberships, company access and cross-tenant isolation.

Two independent firms run in one database: the preview organization ("A", signed in through the
development preview) and "Firm B" (created through the identity service, with its own session).
Every test attacks B's data from A's session, using B's real ids.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.auth import issue_session
from app.core.errors import BadRequest, Conflict, Unauthorized
from app.repositories.identity import IdentityRepository
from app.models import Record, json_field
from app.repositories.records import RecordRepository
from app.services.identity_service import add_member, create_organization
from app.services.intelligence_service import run_scheduled_reminders
from tests.conftest import COOKIE, Api

A, B = "local-preview", "firm-b"
TODAY = date.today().isoformat()


def services_of(client):
    return client.app.state.services


def session_api(client, membership_id: str) -> Api:
    api = Api(client)
    api.cookie = issue_session(services_of(client), membership_id)
    return api


def membership_of(client, organization_id: str, member_id: str):
    with services_of(client).db.read() as session:
        return IdentityRepository(session).membership_by_member(organization_id, member_id)


def seed(api: Api, label: str) -> dict:
    """A small but complete workspace: company, task, document, request, invoice, thread, comment."""
    company = api.create("company", {"name": f"{label} company"})
    doc = api.upload(company, content=f"{label} secret bytes".encode(), shared="true")
    task = api.create("task", {"company": company, "title": f"{label} task", "due": TODAY, "assignee": "owner", "evidence": [doc]})
    request = api.create("request", {"company": company, "title": f"{label} request", "due": TODAY, "assignee": "owner"})
    api.action(company, "comment", {"content": f"{label} STAFF ONLY note"})
    api.portal({"action": "companySave", "id": company, "revision": api.get(company)["revision"], "data": {"name": f"{label} company", "address": "Road"}})
    invoice = api.portal({"action": "invoiceCreate", "company": company, "data": {
        "number": f"{label}-1", "due": TODAY, "items": [{"description": "Fee", "quantity": 1, "rate": "100"}], "seller": {"name": "Firm", "address": "St"}}})["id"]
    thread = api.portal({"action": "threadCreate", "company": company, "subject": f"{label} thread", "content": "Hello", "attachments": [doc]})["id"]
    pack = api.action(task, "pack")["id"]
    return {"company": company, "doc": doc, "task": task, "request": request, "invoice": invoice, "thread": thread, "pack": pack}


@pytest.fixture
def firms(api, client):
    """Organization A (preview) and organization B, each with its own data and owner session."""
    services = services_of(client)
    with services.db.write() as session:
        _, owner_b = create_organization(session, B, "Firm B")
    b = session_api(client, owner_b.id)
    return {"a": api, "b": b, "A": seed(api, "AAA"), "B": seed(b, "BBB"), "services": services}


# A. Organizations and memberships -----------------------------------------------------------------

def test_organization_creation_and_owner_membership(client):
    with services_of(client).db.write() as session:
        organization, membership = create_organization(session, "firm-c", "Firm C", owner_name="Chitra")
        resolved = IdentityRepository(session).resolve(membership.id)
    assert organization.status == "active" and organization.created_at and organization.updated_at
    assert resolved.role == "Owner" and resolved.all_companies and resolved.active and resolved.member_id == "owner"
    with pytest.raises(BadRequest):
        with services_of(client).db.write() as session:
            create_organization(session, "firm-c", "Duplicate")


def test_duplicate_memberships_are_prevented(client):
    db = services_of(client).db
    with db.write() as session:
        repo = IdentityRepository(session)
        user, other = repo.create_user("Ravi"), repo.create_user("Someone else")
        repo.create_membership(A, user.id, "ravi", "Secretary", True)
    with pytest.raises(Conflict):  # same user twice in one organization
        with db.write() as session:
            IdentityRepository(session).create_membership(A, user.id, "ravi-2", "Accountant", True)
    with pytest.raises(Conflict):  # same profile id twice in one organization
        with db.write() as session:
            IdentityRepository(session).create_membership(A, other.id, "owner", "Secretary", True)
    with db.write() as session:  # the same person may belong to another organization
        create_organization(session, "firm-d", "Firm D")
        IdentityRepository(session).create_membership("firm-d", user.id, "ravi", "Client", False)


def test_role_and_scope_constraints_are_enforced_by_the_database(client):
    with services_of(client).db.write() as session:
        user = IdentityRepository(session).create_user("Constraint check")
        with pytest.raises(BadRequest):
            IdentityRepository(session).create_membership(A, user.id, "x", "Superuser", False)
    for statement in (
        "INSERT INTO organization_memberships VALUES ('m1', 'local-preview', '{u}', 'p1', 'Superuser', 'active', 0, 't', 't')",
        "INSERT INTO organization_memberships VALUES ('m2', 'local-preview', '{u}', 'p2', 'Client', 'active', 1, 't', 't')",
        "INSERT INTO organization_memberships VALUES ('m3', 'no-such-org', '{u}', 'p3', 'Secretary', 'active', 0, 't', 't')",
        "INSERT INTO organization_memberships VALUES ('m4', 'local-preview', '{u}', 'p4', 'Secretary', 'suspended', 0, 't', 't')",
    ):
        with pytest.raises(IntegrityError):
            with services_of(client).db.write() as session:
                session.execute(text(statement.format(u=user.id)))


def test_email_is_unique_regardless_of_case(client):
    with services_of(client).db.write() as session:
        IdentityRepository(session).create_user("One", email="ca@firm.example")
    with pytest.raises(BadRequest):
        with services_of(client).db.write() as session:
            IdentityRepository(session).create_user("Two", email="CA@Firm.Example")
    with services_of(client).db.write() as session:  # several users without email are fine
        IdentityRepository(session).create_user("No email 1")
        IdentityRepository(session).create_user("No email 2")


@pytest.mark.parametrize("what", ["membership", "user", "organization"])
def test_inactive_identities_are_rejected(api, client, what):
    staff = api.v2({"action": "member", "name": "Temp staff", "role": "Secretary", "companies": ["*"]})["id"]
    membership = membership_of(client, A, staff)
    temp = session_api(client, membership.id)
    assert temp.state()["actor"]["id"] == staff
    with services_of(client).db.write() as session:
        repo = IdentityRepository(session)
        if what == "membership":
            repo.set_membership_status(membership.id, "inactive")
        elif what == "user":
            repo.set_user_status(membership.user_id, "inactive")
        else:
            repo.set_organization_status(A, "inactive")
    response = temp.request("GET", "/api/v2")
    assert response.status_code == 403
    assert response.json()["error"] in ("This workspace profile is not active.", "This organization is not active.")
    if what != "organization":
        assert api.request("POST", "/api/session", json={"actor": staff}).status_code == 403
        with pytest.raises(Unauthorized):
            issue_session(services_of(client), membership.id)


def test_sessions_without_a_valid_membership_fail_closed(api, client):
    with services_of(client).db.write() as session:
        session.execute(text("INSERT INTO sessions (token_hash, workspace, actor, created, expires) "
                             "VALUES (:h, 'local-preview', 'ghost', 't', '2999-01-01T00:00:00.000Z')"),
                        {"h": __import__("hashlib").sha256(b"legacy-token").hexdigest()})
    client.cookies.clear()
    assert client.get("/api/v2", headers={"cookie": f"{COOKIE}=legacy-token"}).status_code == 401


def test_authorization_comes_from_the_membership_not_the_profile_record(api, client):
    """Editing a profile record directly (e.g. a tampered row) must not grant more access."""
    c1 = api.create("company", {"name": "Allowed"})
    c2 = api.create("company", {"name": "Not allowed"})
    staff = api.v2({"action": "member", "name": "Scoped accountant", "role": "Accountant", "companies": [c1]})["id"]
    with services_of(client).db.write() as session:  # tamper with the stored profile document directly
        repo = RecordRepository(session, A)
        profile = repo.get(staff)
        assert repo.update_if_revision(staff, profile["revision"], {**profile, "role": "Owner", "companies": ["*"]})
    scoped = session_api(client, membership_of(client, A, staff).id)
    state = scoped.state()
    assert state["actor"]["role"] == "Accountant" and state["actor"]["companies"] == [c1]
    assert {r["id"] for r in state["records"] if r["kind"] == "company"} == {c1}
    scoped.v2({"action": "member", "name": "x", "role": "Secretary", "companies": ["*"]}, 403)
    scoped.create("task", {"company": c2, "title": "T", "due": TODAY}, 403)


# B. Tenant isolation ------------------------------------------------------------------------------

def ids(state_records) -> set:
    return {r["id"] for r in state_records}


def test_organization_a_cannot_read_organization_b(firms):
    a, B_ids = firms["a"], firms["B"]
    everything = set(B_ids.values())
    assert not everything & ids(a.state()["records"])
    assert not everything & ids(a.call("/api/v2/records?limit=500")["items"])
    for kind in ("company", "task", "document", "invoice"):
        assert a.call(f"/api/v2/records?kind={kind}&company={B_ids['company']}")["total"] == 0
    portal = a.portal()
    assert not everything & {r["id"] for key in ("companies", "documents", "invoices", "threads", "requests") for r in portal[key]}
    csv = a.request("GET", "/api/v2?export=csv").text
    assert "BBB" not in csv and "BBB" not in str(a.state()) and "BBB" not in str(portal)
    assert a.request("GET", f"/api/v2?export=pack&id={B_ids['pack']}").status_code == 404


def test_organization_a_cannot_update_organization_b(firms):
    a, b, B_ids = firms["a"], firms["b"], firms["B"]
    task = b.get(B_ids["task"])
    for body in (
        {"action": "edit", "id": B_ids["task"], "revision": task["revision"], "data": {"title": "Hijacked"}},
        {"action": "transition", "id": B_ids["task"], "revision": task["revision"], "status": "In progress"},
        {"action": "comment", "id": B_ids["company"], "revision": b.get(B_ids["company"])["revision"], "content": "x"},
        {"action": "documentShare", "id": B_ids["doc"], "revision": b.get(B_ids["doc"])["revision"], "shared": False},
        {"action": "respond", "id": B_ids["request"], "revision": b.get(B_ids["request"])["revision"], "response": "x"},
        {"action": "pack", "id": B_ids["task"], "revision": task["revision"]},
    ):
        assert a.request("POST", "/api/v2", json=body).status_code == 404, body["action"]
    company = b.portal()["companies"][0]
    invoice = next(i for i in b.portal()["invoices"] if i["id"] == B_ids["invoice"])
    thread = next(t for t in b.portal()["threads"] if t["id"] == B_ids["thread"])
    for body in (
        {"action": "companySave", "id": B_ids["company"], "revision": company["revision"], "data": {"name": "Hijacked"}},
        {"action": "invoiceIssue", "company": B_ids["company"], "id": B_ids["invoice"], "revision": invoice["revision"]},
        {"action": "threadReply", "company": B_ids["company"], "id": B_ids["thread"], "revision": thread["revision"], "content": "x"},
        {"action": "annualPlan", "company": B_ids["company"], "period": "2026-27"},
    ):
        assert a.request("POST", "/api/portal", json=body).status_code == 404, body["action"]
    assert b.get(B_ids["task"])["title"] == "BBB task" and b.get(B_ids["task"])["revision"] == task["revision"]


def test_organization_a_cannot_delete_or_move_organization_b_records(firms, client):
    """The API has no delete operation; the data layer cannot reach or re-home another tenant's rows."""
    a, B_ids, services = firms["a"], firms["B"], firms["services"]
    assert a.request("DELETE", "/api/v2").status_code == 405
    assert a.request("DELETE", f"/api/v2-files?id={B_ids['doc']}").status_code == 405
    with services.db.write() as session:
        repo = RecordRepository(session, A)
        assert repo.get(B_ids["task"]) is None
        assert repo.update_if_revision(B_ids["task"], 1, {"title": "x"}) is False
        assert repo.patch_where(["task"], {"reviewRequired": True}, where={"company": B_ids["company"]}) == 0
    with pytest.raises(IntegrityError):  # tenant key is immutable at the database level
        with services.db.write() as session:
            session.execute(text("UPDATE records SET owner = 'local-preview' WHERE owner = 'firm-b' AND id = :id"), {"id": B_ids["task"]})
    with pytest.raises(IntegrityError):  # and records can only belong to an existing organization
        with services.db.write() as session:
            session.execute(text("INSERT INTO records VALUES ('nobody', 'x', 'task', '{}', 1, 't')"))


def test_organization_a_cannot_read_organization_b_audit(firms):
    a, b = firms["a"], firms["b"]
    audit_b = b.call("/api/v2/audit?limit=500")["items"]
    assert "BBB company" in {e["subject"] for e in audit_b} and all(e["owner"] == B for e in audit_b)
    for audit in (a.state()["audit"], a.call("/api/v2/audit?limit=500")["items"]):
        assert audit and all(e["owner"] == A for e in audit)
        assert not {e["id"] for e in audit} & {e["id"] for e in audit_b}
        assert not any("BBB" in e["subject"] for e in audit)


def test_new_audit_events_record_the_acting_membership(firms, client):
    a = firms["a"]
    owner = membership_of(client, A, "owner")
    a.create("company", {"name": "Audited"})
    with firms["services"].db.read() as session:
        actor = session.execute(text("SELECT actor_membership_id FROM audit WHERE owner = 'local-preview' AND subject = 'Audited'")).scalar_one()
    assert actor == owner.id


def test_organization_a_cannot_access_organization_b_files(firms, client):
    a, B_ids, services = firms["a"], firms["B"], firms["services"]
    response = a.request("GET", f"/api/v2-files?id={B_ids['doc']}")
    assert response.status_code == 404 and b"BBB secret" not in response.content
    a.upload(B_ids["company"], content=b"x", expect=403)
    assert a.request("GET", f"/api/portal?export=bundle&company={B_ids['company']}").status_code == 404
    # A record in A that points at B's storage key (tampered or mis-copied data) still cannot read B's file.
    b_key = f"{B}/{B_ids['doc']}"
    a_company = firms["A"]["company"]
    with services.db.write() as session:
        session.execute(text("INSERT INTO records VALUES ('local-preview', 'stolen', 'document', :data, 1, '2026-01-01T00:00:00.000Z')"),
                        {"data": f'{{"name":"x.txt","company":"{a_company}","key":"{b_key}","shared":true}}'})
    response = a.request("GET", "/api/v2-files?id=stolen")
    assert response.status_code == 404 and b"BBB secret" not in response.content
    assert "/" not in response.json()["error"]  # no storage paths in errors


def test_organization_a_cannot_access_organization_b_portal(firms):
    a, B_ids = firms["a"], firms["B"]
    for mode in ("bundle", "calendar"):
        assert a.request("GET", f"/api/portal?export={mode}&company={B_ids['company']}").status_code == 404
    for mode, item in (("invoice", B_ids["invoice"]), ("receipt", B_ids["invoice"])):
        assert a.request("GET", f"/api/portal?export={mode}&company={B_ids['company']}&id={item}").status_code == 404
    a.portal({"action": "threadCreate", "company": B_ids["company"], "subject": "x", "content": "y"}, 404)
    a.portal({"action": "companyCreate", "data": {"name": "Mine"}})  # works, in A only
    assert "Mine" not in str(firms["b"].portal())


def test_guessed_ids_from_organization_b_cannot_be_linked(firms):
    a, B_ids, A_ids = firms["a"], firms["B"], firms["A"]
    common = {"company": A_ids["company"], "title": "Link attempt", "due": TODAY}
    a.create("task", {**common, "evidence": [B_ids["doc"]]}, 400)
    a.create("task", {**common, "dependencies": [B_ids["task"]]}, 400)
    a.create("task", {**common, "assignee": "owner"})  # A's own owner is fine
    a.create("submission", {**common, "task": B_ids["task"]}, 400)
    a.portal({"action": "threadCreate", "company": A_ids["company"], "subject": "x", "content": "y", "attachments": [B_ids["doc"]]}, 400)
    a.create("task", {**common, "company": B_ids["company"]}, 403)


def test_forged_organization_identifiers_are_ignored(firms):
    a, b = firms["a"], firms["b"]
    forged = {"x-organization-id": B, "x-tenant-id": B, "x-governance-actor": "owner", "oai-authenticated-user-id": B}
    state = a.request("GET", "/api/v2", headers=forged).json()
    assert "BBB" not in str(state)
    created = a.request("POST", "/api/v2", headers=forged, json={
        "action": "create", "kind": "company", "owner": B, "organization": B, "organization_id": B,
        "data": {"name": "Forged", "owner": B, "organization_id": B}}).json()["id"]
    assert a.get(created) is not None and b.get(created) is None
    assert a.request("POST", "/api/session", json={"actor": "owner", "organization": B}).status_code == 200
    assert "BBB" not in str(a.state())
    assert a.call("/api/v2/records?company=" + firms["B"]["company"] + "&owner=" + B, expect=400)  # unknown params rejected


def test_profile_switching_never_crosses_organizations(firms, client):
    a, b = firms["a"], firms["b"]
    b_staff = b.v2({"action": "member", "name": "B staff", "role": "Secretary", "companies": ["*"]})["id"]
    assert a.request("POST", "/api/session", json={"actor": b_staff}).status_code == 403
    # Profile ids are per organization: "owner" exists in both, and switching keeps A's.
    a.role("owner")
    assert "BBB" not in str(a.state())


def test_pagination_and_search_are_tenant_scoped(firms):
    a, b = firms["a"], firms["b"]
    for i in range(5):
        b.create("task", {"company": firms["B"]["company"], "title": f"BBB extra {i}", "due": TODAY})
    a_tasks = a.call("/api/v2/records?kind=task&limit=2")
    assert all(r["title"].startswith(("AAA", "Link")) or "BBB" not in r["title"] for r in a_tasks["items"])
    total, seen, offset = a_tasks["total"], [], 0
    while offset < total:
        page = a.call(f"/api/v2/records?kind=task&limit=2&offset={offset}")
        seen += page["items"]
        offset += 2
    assert len(seen) == total and not any("BBB" in r["title"] for r in seen)
    assert b.call("/api/v2/records?kind=task")["total"] == 6


# C. Company-level access --------------------------------------------------------------------------

def test_company_access_is_enforced_for_scoped_staff(api, client):
    c1 = api.create("company", {"name": "Scoped one"})
    c2 = api.create("company", {"name": "Scoped two"})
    t2 = api.create("task", {"company": c2, "title": "Hidden task", "due": TODAY})
    staff = api.v2({"action": "member", "name": "Scoped", "role": "Secretary", "companies": [c1]})["id"]
    scoped = session_api(client, membership_of(client, A, staff).id)
    assert scoped.create("task", {"company": c1, "title": "Allowed", "due": TODAY})
    scoped.create("task", {"company": c2, "title": "Denied", "due": TODAY}, 403)
    scoped.v2({"action": "edit", "id": t2, "revision": 1, "data": {"title": "x"}}, 404)
    scoped.upload(c2, expect=403)
    assert scoped.call(f"/api/v2/records?company={c2}")["total"] == 0
    assert all(r.get("company") == c1 or r["id"] == c1 for r in scoped.call("/api/v2/records?limit=500")["items"])


def test_company_access_cannot_reference_another_organizations_company(firms):
    services, B_ids = firms["services"], firms["B"]
    owner_a = None
    with services.db.read() as session:
        owner_a = IdentityRepository(session).membership_by_member(A, "owner")
    with pytest.raises(IntegrityError):  # B's company id under A: no such company in A (foreign key)
        with services.db.write() as session:
            IdentityRepository(session).grant_company(A, owner_a.id, B_ids["company"])
    with pytest.raises(IntegrityError):  # a non-company record cannot be granted
        with services.db.write() as session:
            IdentityRepository(session).grant_company(A, owner_a.id, firms["A"]["task"])


def test_clients_stay_restricted_and_staff_comments_private(api, client):
    c1 = api.create("company", {"name": "Client co"})
    c2 = api.create("company", {"name": "Other co"})
    api.action(c1, "comment", {"content": "STAFF ONLY on client co"})
    client_id = api.v2({"action": "member", "name": "Client user", "role": "Client", "companies": [c1]})["id"]
    portal_user = session_api(client, membership_of(client, A, client_id).id)
    state = portal_user.state()
    assert "STAFF ONLY" not in str(state) and state["members"] == [] and state["audit"] == []
    assert {r["id"] for r in state["records"] if r["kind"] == "company"} == {c1}
    assert portal_user.request("GET", "/api/v2/audit").status_code == 403
    portal_user.portal({"action": "companySave", "id": c2, "revision": 1, "data": {"name": "x"}}, 404)
    created = portal_user.portal({"action": "companyCreate", "data": {"name": "Client's new co"}})["id"]
    assert membership_of(client, A, client_id) is not None
    assert {c["id"] for c in portal_user.portal()["companies"]} == {c1, created}  # access row added with the company


# E. Background processing -------------------------------------------------------------------------

def test_reminders_run_for_every_active_organization_independently(firms, client):
    services = firms["services"]
    with services.db.write() as session:
        create_organization(session, "firm-dormant", "Dormant firm")
        IdentityRepository(session).set_organization_status("firm-dormant", "inactive")
    results = run_scheduled_reminders(services, "worker-1", 60)
    assert set(results) == {A, B} and results[A] >= 2 and results[B] >= 2
    with services.db.read() as session:
        rows = session.execute(select(Record.owner, json_field("task").label("task")).where(Record.kind == "notification")).all()
    for owner, task in rows:
        assert RecordRepository(session, owner).get(task) is not None  # each reminder points to its own tenant's work
    a_notices = [r for r in firms["a"].state()["records"] if r["kind"] == "notification"]
    assert a_notices and all("BBB" not in r["title"] for r in a_notices)
    assert run_scheduled_reminders(services, "worker-1", 60) == {A: 0, B: 0}  # idempotent per day


def test_only_one_process_runs_the_reminder_pass(client):
    services = services_of(client)
    assert run_scheduled_reminders(services, "process-1", 60) is not None
    assert run_scheduled_reminders(services, "process-2", 60) is None  # lease held by process-1
    with services.db.write() as session:
        session.execute(text("UPDATE background_leases SET expires_at = '2000-01-01T00:00:00.000Z'"))
    assert run_scheduled_reminders(services, "process-2", 60) is not None  # expired lease can be taken over


# F. Development preview stays development-only -------------------------------------------------------

def test_preview_sign_in_and_switching_still_work_in_development(api, client):
    assert api.state()["actor"]["role"] == "Owner"
    api.role("secretary")
    assert api.state()["actor"]["id"] == "secretary"
    api.role("owner")
    assert {m["id"] for m in api.state()["members"]} >= {"owner", "secretary", "client", "auditor"}


def test_add_member_requires_existing_companies_of_the_same_organization(client, api):
    with pytest.raises(IntegrityError):
        with services_of(client).db.write() as session:
            add_member(session, A, "ghost-client", "Ghost", "Client", ["no-such-company"])
    assert membership_of(client, A, "ghost-client") is None  # rolled back with the failed grant
