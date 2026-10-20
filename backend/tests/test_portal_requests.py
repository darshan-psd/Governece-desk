"""The client portal's Requests page: one information request, from creation to closure.

The page lists the request rows of the work tracker (``portal_state()["work"]``), so the Work
Tracker and the Requests page read one projection of one persisted record and cannot disagree.
A client answers a request by uploading documents to it (``/api/v2-files`` with ``request``, which
links the document to the request in the same transaction) and submitting it (the existing
``respond`` action); staff review it with the existing ``requestDecision`` action.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.auth import issue_session
from app.core.jsvalues import today as server_today
from app.services import documents_service
from app.services.identity_service import create_organization
from app.services.portal import tracker
from tests.conftest import Api

TODAY = server_today()
TOMORROW = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
STATEMENT = b"%PDF-1.4 September bank statement"


@pytest.fixture
def firm(api):
    """Two companies, each onboarded with its own client, plus a second client on the first one."""
    company = api.portal({"action": "companyCreate", "clientName": "Priya Client",
                          "data": {"name": "Request Co"}})["id"]
    other = api.portal({"action": "companyCreate", "clientName": "Other Client",
                        "data": {"name": "Other Co"}})["id"]
    members = api.state()["members"]
    client = next(m["id"] for m in members if m["role"] == "Client" and company in m["companies"])
    other_client = next(m["id"] for m in members if m["role"] == "Client" and other in m["companies"])
    second = api.v2({"action": "member", "name": "Second Client", "role": "Client",
                     "companies": [company]})["id"]
    return {"api": api, "company": company, "other": other, "client": client,
            "other_client": other_client, "second": second}


def request_for(api: Api, company: str, assignee: str, title: str = "Bank statements") -> str:
    return api.create("request", {"company": company, "title": title, "due": TOMORROW, "assignee": assignee,
                                  "priority": "High", "notes": "Please upload the September bank statements."})


def upload_for(api: Api, company: str, request_id: str, expect: int = 200, filename: str = "statement.pdf",
               content: bytes = STATEMENT, **fields) -> str | None:
    return api.upload(company, content=content, filename=filename, expect=expect, request=request_id, **fields)


def rows(api: Api, request_id: str) -> list[dict]:
    return [i for i in api.portal()["work"] if i["source"] == "request" and i["id"] == request_id]


def row(api: Api, request_id: str) -> dict | None:
    found = rows(api, request_id)
    assert len(found) <= 1, found  # never a duplicate work item for one request
    return found[0] if found else None


def documents(api: Api) -> set[str]:
    return {r["id"] for r in api.state()["records"] if r["kind"] == "document"}


def other_firm(client) -> Api:
    services = client.app.state.services
    with services.db.write() as session:
        _org, owner_b = create_organization(session, "firm-portal-requests", "Firm B")
    session_b = Api(client)
    session_b.cookie = issue_session(services, owner_b.id)
    return session_b


# What the client sees ---------------------------------------------------------------------------

def test_the_assigned_client_sees_the_request_and_what_answering_it_takes(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    item = row(api, request_id)
    assert item["title"] == "Bank statements" and item["company"] == firm["company"]
    assert item["due"] == TOMORROW and item["updated"] and item["status"] == tracker.AWAITING_CLIENT
    assert item["clientAction"] is True and item["nextAction"] == tracker.REQUEST_NEXT_ACTION
    assert item["request"] == {
        "status": "Open", "revision": 1, "description": "Please upload the September bank statements.",
        "priority": "High", "response": None, "correction": None, "answerable": True,
    }
    assert item["documents"] == []


def test_other_clients_and_organizations_can_neither_see_nor_upload_to_it(firm, client):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    before = documents(api)

    # A second client of the same company: the request is not theirs.
    api.role(firm["second"])
    assert row(api, request_id) is None
    forged = api.request("POST", "/api/v2-files", data={"company": firm["company"], "request": request_id},
                         files={"file": ("statement.pdf", STATEMENT)})
    missing = api.request("POST", "/api/v2-files", data={"company": firm["company"], "request": "no-such-request"},
                          files={"file": ("statement.pdf", STATEMENT)})
    # A forged id answers exactly like a missing one: nothing reveals that the request exists.
    assert forged.status_code == missing.status_code == 404
    assert forged.json()["error"] == missing.json()["error"] == "Request not found."
    api.v2({"id": request_id, "revision": 1, "action": "respond", "response": "Mine"}, 404)

    # A client of another company: refused by company access, and by request visibility.
    api.role(firm["other_client"])
    assert row(api, request_id) is None
    upload_for(api, firm["company"], request_id, expect=403)
    upload_for(api, firm["other"], request_id, expect=404)

    # Another organization: the id means nothing there.
    firm_b = other_firm(client)
    company_b = firm_b.portal({"action": "companyCreate", "data": {"name": "Firm B Co"}})["id"]
    assert request_id not in str(firm_b.portal())
    upload_for(firm_b, company_b, request_id, expect=404)
    upload_for(firm_b, firm["company"], request_id, expect=403)

    api.role("owner")
    assert documents(api) == before  # no refused upload left a document behind
    assert api.get(request_id)["evidence"] == [] and api.get(request_id)["revision"] == 1


# Uploads ----------------------------------------------------------------------------------------

def test_an_upload_is_linked_to_its_request_and_only_that_one(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    sibling = request_for(api, firm["company"], firm["client"], title="Director KYC")
    api.role(firm["client"])
    document = upload_for(api, firm["company"], request_id, category="Financial statements", period="2026-27")

    api.role("owner")
    stored = next(r for r in api.state()["records"] if r["id"] == document)
    assert stored["request"] == request_id and stored["company"] == firm["company"]
    assert stored["shared"] is True and stored["uploadedBy"] == firm["client"]  # client uploads are shared, as before
    assert stored["category"] == "Financial statements" and stored["period"] == "2026-27"
    linked = api.get(request_id)
    assert linked["evidence"] == [document]
    assert linked["status"] == "Open"  # uploading alone does not submit the request
    assert api.get(sibling)["evidence"] == []
    assert any(a["action"].startswith("Attached document to request") and a["subject"] == "Bank statements"
               for a in api.state()["audit"])

    api.role(firm["client"])
    assert row(api, request_id)["documents"] == [{"id": document, "name": "statement.pdf"}]
    assert row(api, sibling)["documents"] == []
    assert api.request("GET", f"/api/v2-files?id={document}").content == STATEMENT


def test_an_upload_must_target_the_requests_own_company(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    other_request = request_for(api, firm["other"], firm["other_client"])
    before = documents(api)
    # The owner can see both companies, but a document cannot answer another company's request.
    response = api.request("POST", "/api/v2-files", data={"company": firm["company"], "request": other_request},
                           files={"file": ("statement.pdf", STATEMENT)})
    assert response.status_code == 400
    assert response.json()["error"] == "Upload the document to the request's own company."
    assert documents(api) == before
    assert api.get(request_id)["evidence"] == [] and api.get(other_request)["evidence"] == []


def test_a_request_takes_documents_only_while_an_answer_is_owed(firm, client):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    upload_for(api, firm["company"], request_id)
    api.action(request_id, "respond", {"response": "Statements attached."})
    before = documents(api)
    upload_for(api, firm["company"], request_id, expect=400)  # under review
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Closed"})
    upload_for(api, firm["company"], request_id, expect=400)  # closed
    assert documents(api) == before

    # An auditor cannot upload at all; an ordinary upload without ``request`` is unchanged.
    fresh = request_for(api, firm["company"], firm["client"], title="Fresh request")
    auditor = api.v2({"action": "member", "name": "Scoped auditor", "role": "Auditor",
                      "companies": [firm["company"]]})["id"]
    api.role(auditor)
    upload_for(api, firm["company"], fresh, expect=403)
    api.role(firm["client"])
    plain = api.upload(firm["company"], content=b"Unrelated", filename="notes.txt")
    api.role("owner")
    assert "request" not in next(r for r in api.state()["records"] if r["id"] == plain)
    assert api.get(fresh)["evidence"] == []


def test_a_decision_taken_during_an_upload_refuses_the_upload_cleanly(firm, client, monkeypatch):
    """The link is written with optimistic locking: a request that changed is never overwritten,
    and the refused upload leaves neither a document record nor file bytes behind."""
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    original = documents_service._answerable_request
    # Simulate the request changing between the read and the write (a stale revision).
    monkeypatch.setattr(documents_service, "_answerable_request",
                        lambda ctx, rid, company: {**original(ctx, rid, company), "revision": 0})
    storage = client.app.state.services.storage.root
    files_before = sorted(p.name for p in storage.iterdir())
    before = documents(api)
    api.role(firm["client"])
    response = api.request("POST", "/api/v2-files", data={"company": firm["company"], "request": request_id},
                           files={"file": ("statement.pdf", STATEMENT)})
    assert response.status_code == 409
    api.role("owner")
    assert documents(api) == before
    assert sorted(p.name for p in storage.iterdir()) == files_before
    assert api.get(request_id)["evidence"] == []


# Review, correction and resubmission ------------------------------------------------------------

def test_a_response_needs_a_message_or_a_document(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    response = api.request("POST", "/api/v2", json={"action": "respond", "id": request_id, "revision": 1, "response": ""})
    assert response.status_code == 400
    assert response.json()["error"] == "Provide a response or upload the requested document."
    upload_for(api, firm["company"], request_id)
    api.action(request_id, "respond", {"response": ""})  # the document is the answer
    assert row(api, request_id)["request"]["status"] == "Responded"


def test_request_to_upload_to_review_to_correction_to_resubmission(firm):
    api = firm["api"]
    company, client_id = firm["company"], firm["client"]
    request_id = request_for(api, company, client_id)

    # 1. The client uploads the statement and submits it.
    api.role(client_id)
    first = upload_for(api, company, request_id)
    api.action(request_id, "respond", {"response": "September statement attached."})
    submitted = row(api, request_id)
    assert submitted["status"] == tracker.UNDER_REVIEW and submitted["clientAction"] is False
    assert submitted["request"]["answerable"] is False and submitted["request"]["response"] == "September statement attached."
    assert [e["key"] for e in submitted["timeline"]] == ["created", "respondedAt"]

    # 2. The employee reviews the submission: the response and the uploaded file are on the request.
    api.role("owner")
    review = api.get(request_id)
    assert review["status"] == "Responded" and review["evidence"] == [first]
    assert review["respondedBy"] == client_id and review["response"] == "September statement attached."
    assert api.request("GET", f"/api/v2-files?id={first}").content == STATEMENT
    assert any(r["id"] == request_id for r in api.state()["records"]
               if r["kind"] == "request" and r["status"] == "Responded")  # what the review queue lists

    # 3. The employee asks for a correction.
    api.action(request_id, "requestDecision", {"status": "Returned", "reason": "Page 2 of the statement is missing."})
    api.role(client_id)
    returned = row(api, request_id)
    assert returned["status"] == tracker.CHANGES_REQUESTED and returned["clientAction"] is True
    assert returned["note"] == "Page 2 of the statement is missing."
    assert returned["request"]["correction"] == "Page 2 of the statement is missing."
    assert returned["request"]["answerable"] is True and returned["request"]["status"] == "Returned"

    # 4. The client uploads the corrected document and resubmits.
    corrected = upload_for(api, company, request_id, filename="statement-corrected.pdf",
                           content=b"%PDF-1.4 September bank statement, both pages")
    api.action(request_id, "respond", {"response": "Added page 2."})
    resubmitted = row(api, request_id)
    assert resubmitted["status"] == tracker.UNDER_REVIEW
    assert resubmitted["request"]["correction"] == "Page 2 of the statement is missing."  # what it answers
    assert [d["id"] for d in resubmitted["documents"]] == [first, corrected]

    # 5. The employee accepts it and closes the request.
    api.role("owner")
    assert api.get(request_id)["evidence"] == [first, corrected]
    api.action(request_id, "requestDecision", {"status": "Closed"})
    api.role(client_id)
    closed = row(api, request_id)
    assert closed["status"] == tracker.COMPLETED and closed["clientAction"] is False
    assert closed["request"] == {**closed["request"], "status": "Closed", "correction": None, "answerable": False}
    assert [d["name"] for d in closed["documents"]] == ["statement.pdf", "statement-corrected.pdf"]


def test_the_work_tracker_shows_the_same_persisted_status_at_every_step(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])

    def check(expected: str) -> None:
        api.role("owner")
        persisted = api.get(request_id)["status"]
        assert persisted == expected
        staff_rows = rows(api, request_id)  # staff previewing the portal see the same row
        api.role(firm["client"])
        client_rows = rows(api, request_id)
        assert len(client_rows) == len(staff_rows) == 1
        for item in (client_rows[0], staff_rows[0]):
            assert item["request"]["status"] == persisted
            assert item["status"] == tracker.REQUEST_STATUS[persisted]

    check("Open")
    api.role(firm["client"])
    upload_for(api, firm["company"], request_id)
    check("Open")
    api.action(request_id, "respond", {"response": "Attached."})
    check("Responded")
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Returned", "reason": "Wrong month."})
    check("Returned")
    api.action(request_id, "respond", {"response": "Correct month attached."})
    check("Responded")
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Closed"})
    check("Closed")


# What stays internal ----------------------------------------------------------------------------

def test_internal_review_notes_and_unpublished_documents_stay_internal(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.action(request_id, "comment", {"content": "INTERNAL: reviewer suspects an altered statement"})
    internal = upload_for(api, firm["company"], request_id, filename="internal-check.txt",
                          content=b"Internal reconciliation")  # staff upload, not published
    api.role(firm["client"])
    published = upload_for(api, firm["company"], request_id)
    api.action(request_id, "respond", {"response": "Attached."})
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Returned", "reason": "Please send page 2."})

    api.role(firm["client"])
    portal = api.portal()
    state = api.state()
    for payload in (portal, state):
        text = str(payload)
        assert "INTERNAL: reviewer suspects" not in text
        assert internal not in text and "internal-check.txt" not in text
    for record in [*portal["requests"], *(r for r in state["records"] if r["kind"] == "request")]:
        assert not {"comments", "preparer", "reviewedBy"} & set(record), record
        assert record["reviewNote"] == "Please send page 2."  # the correction itself is client-facing
    assert row(api, request_id)["documents"] == [{"id": published, "name": "statement.pdf"}]
    assert api.request("GET", f"/api/v2-files?id={internal}").status_code == 404

    # Staff still have the whole record.
    api.role("owner")
    staff_view = api.get(request_id)
    assert staff_view["comments"][0]["content"].startswith("INTERNAL") and staff_view["reviewedBy"] == "owner"
    assert staff_view["evidence"] == [internal, published]


def test_closing_a_request_does_not_complete_the_financial_close(firm):
    api = firm["api"]
    close = api.create("close", {"company": firm["company"], "title": "September 2026 close", "due": TOMORROW,
                                 "assignee": "owner", "period": "2026-09"})
    api.move(close, "In progress")
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    upload_for(api, firm["company"], request_id)
    api.action(request_id, "respond", {"response": "Attached."})
    api.role("owner")
    before = api.get(close)
    api.action(request_id, "requestDecision", {"status": "Closed"})
    after = api.get(close)
    assert after["status"] == "In progress" and after["revision"] == before["revision"]
    assert not after.get("completedAt") and not after.get("approval")
    api.role(firm["client"])
    assert all(i["title"] != "September 2026 close" for i in api.portal()["work"])  # and it stays internal


# The rest of the portal -------------------------------------------------------------------------

def test_existing_portal_pages_keep_working_alongside_requests(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    document = upload_for(api, firm["company"], request_id)
    portal = api.portal()
    for key in ("actor", "companies", "documents", "cases", "obligations", "invoices", "payments", "threads",
                "requests", "alerts", "calendar", "documentCategories", "staff", "today", "sources",
                "internalTasks", "work"):
        assert key in portal, key
    # Documents: the request's upload is in the company library like any client upload.
    assert any(d["id"] == document and d["shared"] for d in portal["documents"])
    # Inbox: conversations are unaffected.
    thread = api.portal({"action": "threadCreate", "company": firm["company"], "subject": "Question",
                         "content": "When is the filing due?"})["id"]
    assert any(t["id"] == thread for t in api.portal()["threads"])
    # Downloads and exports still work for the client's own company.
    assert api.request("GET", f"/api/portal?export=bundle&company={firm['company']}").status_code == 200
    assert api.request("GET", f"/api/portal?export=calendar&company={firm['company']}").status_code == 200
    assert api.request("GET", f"/api/v2-files?id={document}").status_code == 200
