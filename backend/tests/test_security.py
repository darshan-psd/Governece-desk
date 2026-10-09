"""Security tests: authentication, client isolation, role enforcement, downloads, self-approval, concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from tests.conftest import COOKIE, Api


@pytest.fixture
def setup(api):
    c = api.create("company", {"name": "Client company"})
    c2 = api.create("company", {"name": "Another company"})
    today = date.today().isoformat()
    shared = api.upload(c, content=b"shared")
    private = api.upload(c, content=b"private")
    foreign = api.upload(c2, content=b"foreign")
    api.action(shared, "documentShare", {"shared": True})
    client = api.v2({"action": "member", "name": "Client user", "role": "Client", "companies": [c]})["id"]
    task = api.create("task", {"company": c, "title": "Internal task", "due": today, "assignee": "owner", "notes": "STAFF-ONLY task note"})
    request = api.create("request", {"company": c, "title": "Client request", "due": today, "assignee": client})
    other_request = api.create("request", {"company": c2, "title": "Other request", "due": today, "assignee": "owner"})
    api.action(c, "comment", {"content": "INTERNAL: client is late on fees"})
    return {"api": api, "c": c, "c2": c2, "client": client, "shared": shared, "private": private, "foreign": foreign,
            "task": task, "request": request, "other_request": other_request, "today": today}


# 1. Unauthenticated access -------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v2"), ("POST", "/api/v2"), ("GET", "/api/portal"), ("POST", "/api/portal"),
    ("GET", "/api/v2-files?id=x"), ("POST", "/api/v2-files"), ("POST", "/api/v2-import"), ("POST", "/api/session"),
    ("GET", "/api/v2?export=csv"), ("GET", "/api/portal?export=bundle&company=x"),
])
def test_requests_without_a_session_are_rejected(client, method, path):
    client.cookies.clear()
    response = client.request(method, path, json={"action": "initialise"} if method == "POST" else None)
    assert response.status_code == 401
    assert response.json() == {"error": "Please sign in to access your workspace."}


def test_unknown_session_cookie_is_not_treated_as_owner(client):
    client.cookies.clear()
    response = client.get("/api/v2", headers={"cookie": f"{COOKIE}=forged-token"})
    assert response.status_code == 401


def test_opening_the_app_issues_a_secure_owner_session(client):
    client.cookies.clear()
    response = client.get("/")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    signed_in = Api(client)
    signed_in.cookie = response.cookies.get(COOKIE)
    assert signed_in.state()["actor"]["role"] == "Owner"
    # A browser that already has a valid session keeps it (no silent reset to Owner).
    signed_in.v2({"action": "initialise"})
    signed_in.role("secretary")
    again = signed_in.request("GET", "/")
    assert COOKIE not in again.cookies
    assert signed_in.state()["actor"]["id"] == "secretary"


def test_local_sign_in_can_be_disabled(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings.governance_auth_mode = "disabled"
    with TestClient(create_app(settings), base_url="http://testserver") as test_client:
        response = test_client.get("/")
        assert COOKIE not in response.cookies
        assert test_client.get("/api/v2").status_code == 401


def test_profile_switching_requires_a_known_active_profile(api):
    assert api.request("POST", "/api/session", json={"actor": "does-not-exist"}).status_code == 403


# 2. Client accessing internal data -------------------------------------------------------------

def test_client_cannot_see_internal_staff_data(setup):
    api = setup["api"]
    api.role(setup["client"])
    state = api.state()
    body = str(state)
    assert "INTERNAL: client is late on fees" not in body  # fixed: company comments were exposed
    assert "STAFF-ONLY task note" not in body
    assert state["members"] == [] and state["audit"] == []
    assert {r["kind"] for r in state["records"]} <= {"company", "document", "request"}
    company = next(r for r in state["records"] if r["kind"] == "company")
    assert "comments" not in company and "approval" not in company
    assert all("key" not in r for r in state["records"])
    ids = {r["id"] for r in state["records"]}
    assert setup["private"] not in ids and setup["task"] not in ids
    portal = api.portal()
    assert "INTERNAL: client is late on fees" not in str(portal) and portal["internalTasks"] == []
    assert api.request("GET", "/api/v2?export=csv").status_code == 403


# 3. Client accessing another company -----------------------------------------------------------

def test_client_cannot_reach_another_company(setup):
    api = setup["api"]
    api.role(setup["client"])
    assert all(r["id"] == setup["c"] or r.get("company") == setup["c"] for r in api.state()["records"])
    assert [c["id"] for c in api.portal()["companies"]] == [setup["c"]]
    api.portal({"action": "companySave", "id": setup["c2"], "revision": 1, "data": {"name": "Hijack"}}, 404)
    api.portal({"action": "threadCreate", "company": setup["c2"], "subject": "x", "content": "y"}, 404)
    assert api.request("GET", f"/api/portal?export=calendar&company={setup['c2']}").status_code == 404
    api.upload(setup["c2"], content=b"x", expect=403)
    api.v2({"action": "respond", "id": setup["other_request"], "revision": 1, "response": "x"}, 404)


# 4. Unauthorized role actions ------------------------------------------------------------------

def test_roles_are_enforced_server_side(setup):
    api, c, today = setup["api"], setup["c"], setup["today"]
    task = {"company": c, "title": "T", "due": today}
    api.role("auditor")
    api.create("task", task, 403)
    api.v2({"action": "member", "name": "X", "role": "Secretary", "companies": ["*"]}, 403)
    api.portal({"action": "invoiceCreate", "company": c, "data": {}}, 403)
    api.role("director")
    api.edit(setup["task"], {"title": "Changed"}, 403)
    api.v2({"action": "initialise"}, 403)
    api.portal(None)  # directors may read the portal
    api.portal({"action": "threadCreate", "company": c, "subject": "x", "content": "y"}, 403)
    api.role("accountant")
    api.create("rule", {**task, "source": "Policy", "frequency": "Monthly"}, 403)
    api.action(setup["shared"], "documentShare", {"shared": False}, 403)
    api.v2({"action": "automation"}, 403)
    api.role(setup["client"])
    api.v2({"action": "briefing"}, 403)
    api.portal({"action": "companyReview", "id": c, "revision": 1, "status": "Verified"}, 403)
    api.portal({"action": "annualPlan", "company": c, "period": "2026-27"}, 403)


# 5. Unauthorized document downloads ------------------------------------------------------------

def test_document_downloads_respect_sharing_and_company_scope(setup):
    api = setup["api"]
    owner_download = api.request("GET", f"/api/v2-files?id={setup['private']}")
    assert owner_download.status_code == 200 and owner_download.content == b"private"
    assert owner_download.headers["x-content-type-options"] == "nosniff"
    api.role(setup["client"])
    assert api.request("GET", f"/api/v2-files?id={setup['shared']}").content == b"shared"
    assert api.request("GET", f"/api/v2-files?id={setup['private']}").status_code == 404
    assert api.request("GET", f"/api/v2-files?id={setup['foreign']}").status_code == 404
    assert api.request("GET", "/api/v2-files?id=missing").status_code == 404
    scoped = setup["api"]
    scoped.role("owner")
    auditor = scoped.v2({"action": "member", "name": "Scoped auditor", "role": "Auditor", "companies": [setup["c2"]]})["id"]
    scoped.role(auditor)
    assert scoped.request("GET", f"/api/v2-files?id={setup['private']}").status_code == 404
    assert scoped.request("GET", f"/api/v2-files?id={setup['foreign']}").status_code == 200


def test_upload_limits_and_file_types(setup):
    api, c = setup["api"], setup["c"]
    api.upload(c, content=b"x" * (10 * 1024 * 1024 + 1), expect=400)
    api.upload(c, content=b"", expect=400)
    api.upload(c, content=b"MZ", filename="tool.exe", expect=400)
    api.upload(c, content=b"not office", filename="sheet.xlsx", expect=400)
    assert api.upload(c, content=b"%PDF-1.7 real", filename="real.pdf")
    response = api.request("POST", "/api/v2-import", data={"company": c, "period": "2026-09", "currency": "INR"},
                           files={"file": ("big.csv", b"account,debit,credit\n" + b"a,1,1\n" * 200000)})
    assert response.status_code == 400 and "1 MB" in response.json()["error"]


# 6. Self-approval ------------------------------------------------------------------------------

def test_preparers_cannot_approve_their_own_work(setup):
    api, c, today = setup["api"], setup["c"], setup["today"]
    evidence = api.upload(c)
    task = api.create("task", {"company": c, "title": "Self", "due": today, "assignee": "owner", "evidence": [evidence]})
    api.move(task, "In progress")
    api.move(task, "Under review")
    error = api.move(task, "Approved", 400)["error"]
    assert "A different team member must approve the work." in error
    rule = api.create("rule", {"company": c, "title": "R", "due": today, "source": "Policy", "frequency": "Monthly"})
    api.action(rule, "ruleStatus", {"status": "Under review"})
    api.action(rule, "ruleStatus", {"status": "Published"}, 400)
    imported = api.trial_balance(c)
    api.action(imported, "reviewImport", expect=400)


# 7. Concurrent edits ---------------------------------------------------------------------------

def test_stale_revision_is_rejected(setup):
    api = setup["api"]
    record = api.get(setup["task"])
    api.v2({"action": "edit", "id": record["id"], "revision": record["revision"], "data": {"title": "First"}})
    api.v2({"action": "edit", "id": record["id"], "revision": record["revision"], "data": {"title": "Second"}}, 409)
    assert api.get(setup["task"])["title"] == "First"


def test_parallel_edits_only_one_wins(setup, client):
    api = setup["api"]
    record = api.get(setup["task"])
    cookie = f"{COOKIE}={api.cookie}"

    def attempt(n: int) -> int:
        return client.post("/api/v2", headers={"cookie": cookie}, json={
            "action": "edit", "id": record["id"], "revision": record["revision"], "data": {"title": f"Writer {n}"},
        }).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(8)))
    assert statuses.count(200) == 1 and statuses.count(409) == 7
    assert api.get(setup["task"])["revision"] == record["revision"] + 1


def test_parallel_invoice_issue_keeps_numbers_unique(setup, client):
    api, c = setup["api"], setup["c"]
    data = {"number": "INV-RACE", "due": setup["today"], "items": [{"description": "Fee", "quantity": 1, "rate": "100"}],
            "seller": {"name": "Firm", "address": "Street"}}
    api.portal({"action": "companySave", "id": c, "revision": api.get(c)["revision"], "data": {"name": "Client company", "address": "1 Road"}})
    invoices = [api.portal({"action": "invoiceCreate", "company": c, "data": data})["id"] for _ in range(4)]
    revisions = {i["id"]: i["revision"] for i in api.portal()["invoices"]}
    cookie = f"{COOKIE}={api.cookie}"

    def issue(invoice_id: str) -> int:
        return client.post("/api/portal", headers={"cookie": cookie}, json={
            "action": "invoiceIssue", "company": c, "id": invoice_id, "revision": revisions[invoice_id],
        }).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(issue, invoices))
    assert statuses.count(200) == 1 and statuses.count(409) == 3


# Transport guards (ported from local/server.ts) ------------------------------------------------

def test_host_origin_and_body_limits(api):
    assert api.request("GET", "/api/v2", headers={"host": "evil.example"}).status_code == 403
    assert api.request("POST", "/api/v2", json={}, headers={"origin": "https://example.invalid"}).status_code == 403
    assert api.request("POST", "/api/v2", json={"action": "x"}, headers={"origin": "http://testserver"}).status_code != 403
    too_big = api.request("POST", "/api/v2-files", content=b"x" * (12 * 1024 * 1024 + 1),
                          headers={"content-type": "application/octet-stream"})
    assert too_big.status_code == 413


def test_unknown_routes_and_actions(api):
    assert api.request("GET", "/api/workspace").status_code == 404
    assert api.request("GET", "/api/files").status_code == 404
    api.v2({"action": "nonsense"}, 404)  # resolves the record first, as the Node backend did
    company = api.create("company", {"name": "Co"})
    api.v2({"action": "nonsense", "id": company, "revision": 1}, 400)
    api.portal({"action": "nonsense", "company": company}, 400)
    response = api.request("POST", "/api/v2", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400
