"""Information requests must reach the client who has to answer them.

A client sees a request only where it is addressed to them (``accessible()`` and the ``requests``
filter in ``portal_state``), so a request addressed to a colleague never arrives. These tests cover
the recipient rule, who may see the request afterwards, and how it surfaces in the portal and the
work tracker.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.auth import issue_session
from app.core.jsvalues import today as server_today
from app.services.identity_service import create_organization
from app.services.portal import tracker
from tests.conftest import Api

TODAY = server_today()
TOMORROW = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()


@pytest.fixture
def firm(api):
    """Two companies, each onboarded with its own client, plus a second client on one of them."""
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


def request_for(api: Api, company: str, assignee: str, title: str = "Bank statements", expect: int = 200):
    return api.create("request", {"company": company, "title": title, "due": TOMORROW,
                                  "assignee": assignee, "priority": "High",
                                  "notes": "Please send the September bank statements."}, expect)


# The recipient rule ----------------------------------------------------------------------------

def test_a_request_must_be_addressed_to_a_client_of_that_company(firm):
    api = firm["api"]
    # Addressing a colleague is refused while the company has a client who could answer.
    response = api.request("POST", "/api/v2", json={"action": "create", "kind": "request", "data": {
        "company": firm["company"], "title": "Bank statements", "due": TOMORROW, "assignee": "owner"}})
    assert response.status_code == 400
    assert response.json()["error"] == "Assign this request to the client who must answer it in the portal."
    for staff in ("owner", "secretary", "accountant", "director", "auditor"):
        request_for(api, firm["company"], staff, expect=400)
    # A client of another company is refused by the existing company-access rule.
    request_for(api, firm["company"], firm["other_client"], expect=400)
    request_for(api, firm["company"], "no-such-member", expect=400)
    # The company's own client is accepted, with every field intact.
    request_id = request_for(api, firm["company"], firm["client"])
    stored = api.get(request_id)
    assert stored["assignee"] == firm["client"] and stored["company"] == firm["company"]
    assert stored["status"] == "Open" and stored["priority"] == "High" and stored["due"] == TOMORROW
    assert stored["notes"] == "Please send the September bank statements."


def test_editing_a_request_keeps_the_recipient_rule(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.edit(request_id, {"assignee": "owner"}, 400)
    api.edit(request_id, {"assignee": firm["other_client"]}, 400)
    assert api.get(request_id)["assignee"] == firm["client"]
    # Reassigning to the company's other client is allowed.
    api.edit(request_id, {"assignee": firm["second"]})
    assert api.get(request_id)["assignee"] == firm["second"]


def test_a_company_without_a_client_can_still_record_a_request(api):
    """Nothing is lost for a company that has no portal client yet."""
    company = api.create("company", {"name": "No Portal Client Ltd"})
    request_id = request_for(api, company, "secretary")
    assert api.get(request_id)["assignee"] == "secretary"


def test_the_recipient_list_a_secretary_sees_marks_clients(firm):
    api = firm["api"]
    api.role("secretary")
    members = {m["id"]: m for m in api.state()["members"]}
    assert members[firm["client"]]["client"] is True
    assert members["owner"]["client"] is False
    assert "role" not in members[firm["client"]]  # the role itself stays owner-only
    api.role("owner")
    assert next(m for m in api.state()["members"] if m["id"] == firm["client"])["client"] is True


# What the client sees ---------------------------------------------------------------------------

def test_the_assigned_client_sees_the_request_in_their_portal(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    portal = api.portal()
    item = next(r for r in portal["requests"] if r["id"] == request_id)
    assert item["title"] == "Bank statements" and item["status"] == "Open" and item["due"] == TOMORROW
    # And in the internal state the client is allowed to read.
    assert any(r["id"] == request_id for r in api.state()["records"])
    # They can answer it, which is the point of receiving it.
    api.action(request_id, "respond", {"response": "Statements attached."})
    assert api.get(request_id)["status"] == "Responded"


def test_a_newly_created_request_appears_on_the_next_portal_fetch(firm):
    """The portal has no stale cache: a refetch after creation shows the request."""
    api = firm["api"]
    api.role(firm["client"])
    assert api.portal()["requests"] == []
    api.role("owner")
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    assert [r["id"] for r in api.portal()["requests"]] == [request_id]
    assert [i["id"] for i in api.portal()["work"] if i["source"] == "request"] == [request_id]


def test_another_client_of_the_same_company_does_not_see_it(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["second"])
    assert all(r["id"] != request_id for r in api.portal()["requests"])
    assert all(i["id"] != request_id for i in api.portal()["work"])
    assert all(r["id"] != request_id for r in api.state()["records"])
    # Nor can they answer it.
    assert api.v2({"id": request_id, "revision": 1, "action": "respond", "response": "Mine"}, 404)


def test_a_client_of_another_company_does_not_see_it(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["other_client"])
    assert all(r["id"] != request_id for r in api.portal()["requests"])
    assert all(i["company"] == firm["other"] for i in api.portal()["work"])


def test_another_organization_cannot_reach_the_request(firm, client):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    services = client.app.state.services
    with services.db.write() as session:
        _org, owner_b = create_organization(session, "firm-requests", "Firm B")
    other_firm = Api(client)
    other_firm.cookie = issue_session(services, owner_b.id)
    assert request_id not in str(other_firm.portal())
    assert other_firm.v2({"id": request_id, "revision": 1, "action": "respond", "response": "x"}, 404)
    # A forged company id does not widen the portal either.
    assert other_firm.request("GET", f"/api/portal?export=bundle&company={firm['company']}").status_code == 404


# The work tracker -------------------------------------------------------------------------------

def test_the_request_tracks_its_real_status_for_the_client(firm):
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.role(firm["client"])
    item = next(i for i in api.portal()["work"] if i["id"] == request_id)
    assert item["status"] == tracker.AWAITING_CLIENT and item["clientAction"] is True
    assert item["category"] == tracker.REQUEST_CATEGORY and item["due"] == TOMORROW
    assert item["nextAction"] == tracker.REQUEST_NEXT_ACTION

    api.action(request_id, "respond", {"response": "Statements attached."})
    assert next(i for i in api.portal()["work"] if i["id"] == request_id)["status"] == tracker.UNDER_REVIEW

    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Returned", "reason": "Please add September."})
    api.role(firm["client"])
    returned = next(i for i in api.portal()["work"] if i["id"] == request_id)
    assert returned["status"] == tracker.CHANGES_REQUESTED and returned["note"] == "Please add September."

    api.action(request_id, "respond", {"response": "September added."})
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Closed"})
    api.role(firm["client"])
    assert next(i for i in api.portal()["work"] if i["id"] == request_id)["status"] == tracker.COMPLETED


def test_existing_request_review_still_works_end_to_end(firm):
    """Regression: the workflow around requests is unchanged."""
    api = firm["api"]
    request_id = request_for(api, firm["company"], firm["client"])
    api.action(request_id, "requestDecision", {"status": "Closed"}, 400)  # nothing responded yet
    api.role(firm["client"])
    api.action(request_id, "respond", {"response": ""}, 400)              # a response is required
    api.action(request_id, "respond", {"response": "Statements attached."})
    api.action(request_id, "requestDecision", {"status": "Closed"}, 403)  # clients cannot decide
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Returned"}, 400)  # a reason is required
    api.action(request_id, "requestDecision", {"status": "Returned", "reason": "Add September."})
    api.role(firm["client"])
    api.action(request_id, "respond", {"response": "September added."})
    api.role("owner")
    api.action(request_id, "requestDecision", {"status": "Closed"})
    closed = api.get(request_id)
    assert closed["status"] == "Closed" and closed["response"] == "September added."
    assert closed["reviewNote"] == ""  # cleared on the closing decision, as before


# The recipient list the form is built from ------------------------------------------------------

def test_the_member_list_offers_exactly_the_clients_of_the_selected_company(firm, client):
    """What the Client recipient dropdown is built from, checked at the API that supplies it."""
    api = firm["api"]
    services = client.app.state.services
    with services.db.write() as session:
        _org, owner_b = create_organization(session, "firm-recipients", "Firm B")
    other_firm = Api(client)
    other_firm.cookie = issue_session(services, owner_b.id)
    other_company = other_firm.portal({"action": "companyCreate", "clientName": "Foreign Client",
                                       "data": {"name": "Foreign Co"}})["id"]

    def recipients(member_list, company):
        """The frontend's rule: a client, active, with access to the chosen company."""
        return sorted(m["name"] for m in member_list
                      if (m.get("client") is True or m.get("role") == "Client")
                      and m.get("active") is not False and company in (m.get("companies") or []))

    for actor in ("owner", "secretary", "accountant"):
        api.role(actor)
        members = api.state()["members"]
        # 1. The authorised clients of this company are offered...
        assert recipients(members, firm["company"]) == ["Priya Client", "Second Client"], actor
        # 2. ...a client of another company of the same firm is not...
        assert "Other Client" not in recipients(members, firm["company"]), actor
        assert recipients(members, firm["other"]) == ["Other Client"], actor
        # 3. ...and another organization's client is never in the payload at all.
        assert all(m["name"] != "Foreign Client" for m in members), actor
        assert other_company not in str(members), actor
        assert all("client" in m for m in members), actor  # the flag the form reads
    api.role("owner")

    # 4. The request can then be assigned to one of the offered clients, and only to those.
    request_id = request_for(api, firm["company"], firm["client"])
    api.edit(request_id, {"assignee": firm["second"]})
    assert api.get(request_id)["assignee"] == firm["second"]
    api.edit(request_id, {"assignee": firm["other_client"]}, 400)

    # 5. Visibility is unchanged by any of this: only the current recipient sees it.
    api.role(firm["second"])
    assert [r["id"] for r in api.portal()["requests"]] == [request_id]
    api.role(firm["client"])
    assert api.portal()["requests"] == []
    assert other_firm.portal()["requests"] == []


def test_a_client_marked_inactive_is_no_longer_offered(firm):
    api = firm["api"]
    api.call("/api/auth/team/status", {"member": firm["second"], "status": "inactive"})
    members = {m["name"]: m for m in api.state()["members"]}
    assert members["Second Client"]["active"] is False      # the form filters this one out
    assert members["Priya Client"]["active"] is True
    request_for(api, firm["company"], firm["second"], expect=400)  # and the server refuses it
    assert request_for(api, firm["company"], firm["client"])
