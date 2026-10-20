"""Client-portal work tracker: the projection itself, and what a real client sees through the API.

The projection is a pure function over one ``portal_state()`` payload, so most rules are tested
directly against it; the API tests then prove the payload it reads is the authorised one (company
scope, organization scope, the client's own requests, published documents only).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.auth import issue_session
from app.core.jsvalues import today as server_today
from app.services.identity_service import create_organization
from app.services.portal import tracker
from tests.conftest import Api

# The server decides what "today" is (UTC), and overdue is defined against it; a local date would
# make these tests pass or fail depending on the machine's time zone.
TODAY = server_today()
YESTERDAY = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
TOMORROW = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()


def state(**overrides) -> dict:
    """A minimal portal state, shaped exactly like ``portal_state()`` output."""
    return {"today": TODAY, "documents": [], "obligations": [], "cases": [], "requests": [], **overrides}


def obligation(**fields) -> dict:
    return {"id": "ob-1", "kind": "portalobligation", "company": "c1", "title": "Annual return",
            "status": "Not started", "due": TOMORROW, "updated": "2026-10-01T00:00:00.000Z", **fields}


def one(payload: dict) -> dict:
    items = tracker.work_items(payload)
    assert len(items) == 1, items
    return items[0]


# The projection: sources ---------------------------------------------------------------------

def test_a_compliance_item_becomes_a_work_item():
    item = one(state(obligations=[obligation(period="2026-27", category="Annual compliance",
                                             note="We will confirm the filing date.")]))
    assert item == {
        "id": "ob-1", "source": "obligation", "title": "Annual return", "company": "c1",
        "category": "Annual compliance", "period": "2026-27", "status": tracker.NOT_STARTED,
        "statusNote": tracker.STATUS_NOTES[tracker.NOT_STARTED], "updated": "2026-10-01T00:00:00.000Z",
        "due": TOMORROW, "overdue": False, "clientAction": False,
        "nextAction": tracker.NO_ACTION, "documents": [],
        "note": "We will confirm the filing date.",
        # No workflow timestamp exists for a hand-maintained compliance item, so none is invented.
        "completed": None, "cancelled": None, "timeline": [],
    }


def test_onboarding_becomes_one_item_with_its_real_milestones():
    case = {"id": "onboard-c1", "company": "c1", "title": "Company registration & onboarding",
            "updated": "2026-10-02T00:00:00.000Z", "stages": [
                {"id": "engagement", "title": "Engagement", "description": "Confirm scope.",
                 "status": "Verified", "due": YESTERDAY, "updated": "2026-10-02T00:00:00.000Z",
                 "note": "Signed.", "evidence": []},
                {"id": "verification", "title": "Identity review", "status": "Waiting for client",
                 "due": TOMORROW, "note": "Please send director ID.", "evidence": []},
                {"id": "constitution", "title": "Name & incorporation", "status": "Not started", "due": ""},
            ]}
    item = one(state(cases=[case]))
    assert item["source"] == "onboarding" and item["id"] == "onboard-c1"
    assert item["status"] == tracker.AWAITING_CLIENT and item["clientAction"] is True
    assert item["progress"] == {"verified": 1, "total": 3}  # persisted milestone count, not a guess
    assert item["due"] == TOMORROW  # the next unverified milestone, not the verified one in the past
    assert item["note"] == "Please send director ID." and item["updated"] == "2026-10-02T00:00:00.000Z"
    assert [m["status"] for m in item["milestones"]] == [tracker.COMPLETED, tracker.AWAITING_CLIENT, tracker.NOT_STARTED]
    assert item["milestones"][2]["due"] is None and item["milestones"][2]["updated"] is None
    assert "reviewedBy" not in str(item)


def test_onboarding_status_is_derived_from_the_stored_stages():
    def case(*statuses):
        return {"id": "onboard-c1", "company": "c1", "title": "Onboarding",
                "stages": [{"id": f"s{i}", "title": f"Stage {i}", "status": s} for i, s in enumerate(statuses)]}
    assert one(state(cases=[case("Not started", "Not started")]))["status"] == tracker.NOT_STARTED
    assert one(state(cases=[case("Verified", "Not started")]))["status"] == tracker.IN_PROGRESS
    assert one(state(cases=[case("In progress", "Not started")]))["status"] == tracker.IN_PROGRESS
    assert one(state(cases=[case("Verified", "Review required")]))["status"] == tracker.UNDER_REVIEW
    assert one(state(cases=[case("Verified", "Verified")]))["status"] == tracker.COMPLETED
    # A stage waiting on the client outranks everything else.
    assert one(state(cases=[case("Review required", "Waiting for client")]))["status"] == tracker.AWAITING_CLIENT


def test_a_request_becomes_a_work_item_with_the_correction_note():
    item = one(state(requests=[{"id": "rq-1", "company": "c1", "title": "Bank statements",
                                "status": "Returned", "due": TOMORROW, "reviewNote": "Please add September.",
                                "response": "Earlier answer", "updated": "2026-10-03T00:00:00.000Z"}]))
    assert item["status"] == tracker.CHANGES_REQUESTED and item["clientAction"] is True
    assert item["note"] == "Please add September." and item["category"] == tracker.REQUEST_CATEGORY
    assert item["nextAction"] == tracker.REQUEST_NEXT_ACTION
    # An open request names the client, so it may say the client must act.
    assert one(state(requests=[{"id": "rq-2", "company": "c1", "title": "Ledgers", "status": "Open"}]))["status"] == tracker.AWAITING_CLIENT


def test_invoices_and_payments_are_not_a_source_in_this_phase():
    payload = state(invoices=[{"id": "inv-1", "company": "c1", "status": "Issued", "number": "INV-1",
                               "due": TOMORROW, "balance": 5000}],
                    payments=[{"id": "pay-1", "company": "c1", "status": "Pending verification"}])
    assert tracker.work_items(payload) == []


# The projection: status mapping ----------------------------------------------------------------

@pytest.mark.parametrize("stored,expected", [
    ("Not started", tracker.NOT_STARTED),
    ("In progress", tracker.IN_PROGRESS),
    ("Waiting for client", tracker.AWAITING_CLIENT),
    ("Needs scheduling", tracker.NEEDS_SCHEDULING),
    ("Completed", tracker.COMPLETED),
    ("Not applicable", tracker.NOT_APPLICABLE),
    ("Cancelled", tracker.CANCELLED),
    ("Under review", tracker.UNDER_REVIEW),
    ("Review required", tracker.UNDER_REVIEW),
])
def test_obligation_status_mapping(stored, expected):
    assert one(state(obligations=[obligation(status=stored)]))["status"] == expected


def test_ambiguous_internal_statuses_are_mapped_conservatively():
    """Two internal statuses must not be over-claimed to a client."""
    # "Awaiting information" does not record who owes the information, so it is not client action.
    awaiting = one(state(obligations=[obligation(status="Awaiting information")]))
    assert awaiting["status"] == tracker.IN_PROGRESS and awaiting["clientAction"] is False
    # An internal approval is not completion.
    approved = one(state(obligations=[obligation(status="Approved")]))
    assert approved["status"] == tracker.UNDER_REVIEW and approved["status"] != tracker.COMPLETED
    # "Not applicable" stays its own outcome rather than being folded into Completed.
    assert one(state(obligations=[obligation(status="Not applicable")]))["status"] == tracker.NOT_APPLICABLE


def test_an_unknown_status_is_reported_as_no_status_rather_than_guessed():
    item = one(state(obligations=[obligation(status="Something new", due=YESTERDAY)]))
    assert item["status"] is None and item["clientAction"] is False
    assert item["statusNote"] == "Your secretary has not reported a status for this item yet."
    assert item["nextAction"] is None
    assert item["overdue"] is True  # a real past due date on unsettled work is still late
    for blank in ("", None):
        assert one(state(obligations=[obligation(status=blank)]))["status"] is None


def test_client_action_is_only_claimed_where_the_data_says_so():
    acting = {"Waiting for client"}
    for stored in tracker.OBLIGATION_STATUS:
        item = one(state(obligations=[obligation(status=stored)]))
        assert item["clientAction"] is (stored in acting), stored
        assert (item["status"] in tracker.CLIENT_ACTION) is (stored in acting), stored


# The projection: dates and optional fields -----------------------------------------------------

def test_overdue_uses_the_servers_today_and_spares_items_due_today():
    assert one(state(obligations=[obligation(due=YESTERDAY)]))["overdue"] is True
    assert one(state(obligations=[obligation(due=TODAY)]))["overdue"] is False
    assert one(state(obligations=[obligation(due=TOMORROW)]))["overdue"] is False
    for settled in ("Completed", "Cancelled", "Not applicable"):
        assert one(state(obligations=[obligation(due=YESTERDAY, status=settled)]))["overdue"] is False
    # An explicit "today" overrides the payload's, and a browser date is never used.
    assert tracker.work_items(state(obligations=[obligation(due=TODAY)]), today=TOMORROW)[0]["overdue"] is True


def test_missing_and_invalid_optional_fields_become_null():
    item = one(state(obligations=[{"id": "ob-1", "company": "c1", "title": "Sparse item", "status": "In progress"}]))
    assert item["due"] is None and item["period"] is None and item["note"] is None and item["updated"] is None
    assert item["documents"] == [] and item["overdue"] is False
    for bad_due in ("2026-02-30", "soon", "", None):
        assert one(state(obligations=[obligation(due=bad_due)]))["due"] is None
    assert one(state(obligations=[obligation(company=None)]))["company"] is None


def test_published_documents_only_and_never_the_internal_link():
    payload = state(
        documents=[{"id": "d-shared", "name": "Certificate.pdf", "company": "c1", "shared": True},
                   {"id": "d-internal", "name": "Internal notes.pdf", "company": "c1", "shared": False}],
        obligations=[obligation(evidence=["d-shared", "d-internal", "d-missing", 7],
                                sourceTask="task-internal-1", reference="MCA-REF-9")])
    item = one(payload)
    assert item["documents"] == [{"id": "d-shared", "name": "Certificate.pdf"}]
    for leak in ("d-internal", "Internal notes", "task-internal-1", "sourceTask", "MCA-REF-9"):
        assert leak not in str(item), leak


def test_the_row_carries_only_the_approved_fields():
    noisy = obligation(sourceTask="task-1", reference="REF-1", filing=True, source="Internal policy",
                       cancellationReason="internal reason", preparer="owner", approval={"actor": "owner"},
                       comments=[{"content": "internal chatter"}], key="local-preview/secret")
    assert set(one(state(obligations=[noisy]))) == {
        "id", "source", "title", "company", "category", "period", "status", "statusNote",
        "updated", "due", "overdue", "clientAction", "nextAction", "documents", "note",
        "completed", "cancelled", "timeline"}


def test_items_are_ordered_and_deduplicated():
    payload = state(obligations=[obligation(id="ob-late", title="Late", due=YESTERDAY),
                                 obligation(id="ob-soon", title="Soon", due=TOMORROW),
                                 obligation(id="ob-act", title="Act", status="Waiting for client", due="2030-01-01"),
                                 obligation(id="ob-soon", title="Duplicate id", due=TOMORROW)])
    items = tracker.work_items(payload)
    assert [i["title"] for i in items] == ["Act", "Late", "Soon"]  # action first, then late, then by due date


# Through the API, as a real client -------------------------------------------------------------

@pytest.fixture
def portal(api):
    """One company with onboarding, a published internal task, a client request and two documents."""
    company = api.portal({"action": "companyCreate", "clientName": "Tracker client",
                          "data": {"name": "Tracker Co"}})["id"]
    other = api.portal({"action": "companyCreate", "clientName": "Other client",
                        "data": {"name": "Other Co"}})["id"]
    client_id = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and company in m["companies"])
    other_client = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and other in m["companies"])
    published = api.upload(company, content=b"Certificate", filename="certificate.txt",
                           category="Incorporation certificate", shared="true")
    internal = api.upload(company, content=b"Internal only", filename="internal.txt", shared="false")
    task = api.create("task", {"company": company, "title": "Annual return preparation",
                               "due": TOMORROW, "assignee": "owner"})
    api.portal({"action": "publishTask", "company": company, "task": task})
    api.portal({"action": "obligationCreate", "company": company, "data": {
        "title": "Director KYC", "due": YESTERDAY, "source": "Internal policy", "status": "Waiting for client",
        "note": "Please confirm the director details.", "period": "2026-27", "evidence": [published]}})
    request = api.create("request", {"company": company, "title": "Bank statements",
                                     "due": TOMORROW, "assignee": client_id})
    api.create("request", {"company": other, "title": "Other company request",
                           "due": TOMORROW, "assignee": other_client})
    return {"api": api, "company": company, "other": other, "client": client_id,
            "other_client": other_client, "task": task, "request": request,
            "published": published, "internal": internal}


def test_a_client_sees_their_own_work_only(portal):
    api = portal["api"]
    api.role(portal["client"])
    items = api.portal()["work"]
    titles = {i["title"] for i in items}
    assert "Annual return preparation" in titles      # published internal work
    assert "Director KYC" in titles                   # compliance item
    assert "Bank statements" in titles                # their own request
    assert "Company registration & onboarding" in titles
    assert "Other company request" not in titles      # another client's request
    assert all(i["company"] == portal["company"] for i in items)
    assert {i["source"] for i in items} <= {"obligation", "onboarding", "request"}


def test_a_published_task_appears_once_and_mirrors_its_status(portal):
    api = portal["api"]
    api.move(portal["task"], "In progress")
    api.role(portal["client"])
    published = [i for i in api.portal()["work"] if i["title"] == "Annual return preparation"]
    assert len(published) == 1  # not once as a task and once as a compliance item
    assert published[0]["status"] == tracker.IN_PROGRESS and published[0]["source"] == "obligation"
    api.role("owner")
    api.action(portal["task"], "transition", {"status": "Cancelled", "reason": "No longer required."})
    api.role(portal["client"])
    cancelled = next(i for i in api.portal()["work"] if i["title"] == "Annual return preparation")
    assert cancelled["status"] == tracker.CANCELLED and cancelled["overdue"] is False
    assert "No longer required." not in str(cancelled)  # the internal reason stays internal


def test_only_published_documents_reach_the_tracker(portal):
    api = portal["api"]
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["title"] == "Director KYC")
    assert item["documents"] == [{"id": portal["published"], "name": "certificate.txt"}]
    assert item["clientAction"] is True and item["overdue"] is True and item["period"] == "2026-27"
    # Withdrawing publication removes it from the tracker as well.
    api.role("owner")
    api.action(portal["published"], "documentShare", {"shared": False})
    api.role(portal["client"])
    assert next(i for i in api.portal()["work"] if i["title"] == "Director KYC")["documents"] == []


def test_work_items_never_name_another_company_or_organization(portal, client):
    api = portal["api"]
    services = client.app.state.services
    with services.db.write() as session:
        _org, owner_b = create_organization(session, "firm-tracker", "Firm B")
    other_firm = Api(client)
    other_firm.cookie = issue_session(services, owner_b.id)
    other_firm.portal({"action": "companyCreate", "data": {"name": "Firm B Co"}})
    assert all(i["title"] != "Firm B Co" for i in api.portal()["work"])
    assert "Tracker Co" not in str(other_firm.portal()["work"])

    # A forged company id cannot widen the tracker: the portal refuses the company outright.
    api.role(portal["client"])
    assert api.request("GET", f"/api/portal?export=bundle&company={portal['other']}").status_code == 404
    assert all(i["company"] == portal["company"] for i in api.portal()["work"])


def test_the_existing_portal_response_is_unchanged(portal):
    api = portal["api"]
    api.role(portal["client"])
    payload = api.portal()
    for key in ("actor", "companies", "documents", "cases", "obligations", "invoices", "payments",
                "threads", "requests", "alerts", "calendar", "documentCategories", "staff", "today",
                "sources", "internalTasks"):
        assert key in payload, key
    assert payload["internalTasks"] == []  # still staff-only
    assert isinstance(payload["work"], list) and payload["work"]
    # Staff previewing the portal get the same client-facing projection.
    api.role("owner")
    staff_payload = api.portal()
    assert staff_payload["staff"] is True
    assert {i["id"] for i in staff_payload["work"]} >= {i["id"] for i in payload["work"]}
    assert all(d["id"] != portal["internal"] for i in staff_payload["work"] for d in i["documents"])


# Publishing internal work to a client's tracker -------------------------------------------------

def approved_close(portal: dict, title: str = "January 2026 Financial Close") -> str:
    """A financial close taken through its real workflow to Completed.

    The close review gate refuses approval while the company has an unresolved information
    request, so the fixture's open request is answered and closed first, as it would be in practice.
    """
    api, company = portal["api"], portal["company"]
    for request in [r for r in api.state()["records"]
                    if r["kind"] == "request" and r.get("company") == company and r.get("status") != "Closed"]:
        api.role(portal["client"])
        api.action(request["id"], "respond", {"response": "Statements attached."})
        api.role("owner")
        api.action(request["id"], "requestDecision", {"status": "Closed"})
    evidence = api.upload(company, content=b"Close evidence", filename="close.txt")
    imported = api.trial_balance(company, period="2026-01")
    api.role("accountant")
    api.action(imported, "reviewImport")
    api.role("owner")
    close = api.create("close", {"company": company, "title": title, "due": TOMORROW, "assignee": "owner",
                                 "period": "2026-01", "trialbalance": imported, "evidence": [evidence],
                                 "checklist": [{"label": "Review variance", "done": True}]})
    api.move(close, "In progress")
    api.move(close, "Under review")
    api.role("secretary")
    api.move(close, "Approved")
    api.move(close, "Completed")
    api.role("owner")
    assert api.get(close)["status"] == "Completed"
    return close


def test_an_unpublished_financial_close_never_reaches_the_client(portal):
    """The reported symptom: completing a close does not by itself make it client-visible."""
    api = portal["api"]
    close = approved_close(portal)
    api.role(portal["client"])
    payload = api.portal()
    assert all(i["title"] != "January 2026 Financial Close" for i in payload["work"])
    assert close not in str(payload)  # the internal record is not in the client's portal at all
    assert payload["internalTasks"] == []


def test_publishing_a_completed_close_puts_it_in_the_tracker(portal):
    api = portal["api"]
    close = approved_close(portal)
    offered = {t["id"]: t for t in api.portal()["internalTasks"]}
    assert offered[close]["kind"] == "close" and offered[close]["category"] == "Financial close"
    assert offered[close]["period"] == "2026-01"

    api.portal({"action": "publishTask", "company": portal["company"], "task": close,
                "note": "Your January accounts are closed."})
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["title"] == "January 2026 Financial Close")
    assert item["source"] == "obligation" and item["status"] == tracker.COMPLETED
    assert item["category"] == "Financial close" and item["period"] == "2026-01"
    assert item["overdue"] is False and item["clientAction"] is False
    assert item["note"] == "Your January accounts are closed."
    assert item["documents"] == []  # internal close evidence is not published by publishing the work
    for internal in ("trialbalance", "checklist", "approval", "sourceTask", "preparer"):
        assert internal not in str(item), internal
    # The published item keeps the existing deterministic id (``portal-<source id>``, which the
    # duplicate guard relies on), and that id buys the client nothing: the close itself stays
    # unreadable, and no internal field of it is in the portal payload.
    assert item["id"] == f"portal-{close}"
    assert api.v2({"id": close, "revision": 1, "action": "comment", "content": "probe"}, 404)
    assert "Review variance" not in str(api.portal())


@pytest.mark.parametrize("kind,category,extra", [
    ("meeting", "Board meeting", {"quorum": 1, "agenda": "Approve accounts"}),
    ("event", "Corporate event", {"eventType": "Director appointment", "effective": TOMORROW}),
])
def test_other_internal_work_can_be_published_too(portal, kind, category, extra):
    api = portal["api"]
    record = api.create(kind, {"company": portal["company"], "title": f"Published {kind}", "due": TOMORROW,
                               "assignee": "owner", **extra})
    api.portal({"action": "publishTask", "company": portal["company"], "task": record})
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["title"] == f"Published {kind}")
    assert item["category"] == category and item["status"] in (tracker.NOT_STARTED, tracker.IN_PROGRESS)


def test_publication_keeps_its_existing_guards(portal):
    api = portal["api"]
    close = approved_close(portal)
    api.portal({"action": "publishTask", "company": portal["company"], "task": close})
    api.portal({"action": "publishTask", "company": portal["company"], "task": close}, 400)  # no duplicates
    # Another company's work cannot be published into this company's portal.
    foreign = api.create("task", {"company": portal["other"], "title": "Other company work",
                                  "due": TOMORROW, "assignee": "owner"})
    api.portal({"action": "publishTask", "company": portal["company"], "task": foreign}, 400)
    # Kinds that are not approved for publication are refused.
    rule = api.create("rule", {"company": portal["company"], "title": "Monthly review", "due": TOMORROW,
                               "assignee": "owner", "frequency": "Monthly", "offset": 0,
                               "source": "Internal policy"})
    api.portal({"action": "publishTask", "company": portal["company"], "task": rule}, 400)
    document = api.upload(portal["company"], content=b"x", filename="x.txt")
    api.portal({"action": "publishTask", "company": portal["company"], "task": document}, 400)
    # Cancelled work cannot be published.
    cancelled = api.create("task", {"company": portal["company"], "title": "Dropped work",
                                    "due": TOMORROW, "assignee": "owner"})
    api.action(cancelled, "transition", {"status": "Cancelled", "reason": "Not required."})
    api.portal({"action": "publishTask", "company": portal["company"], "task": cancelled}, 400)
    # A client cannot publish anything, and cannot see the internal work on offer.
    api.role(portal["client"])
    api.portal({"action": "publishTask", "company": portal["company"], "task": close}, 403)
    assert api.portal()["internalTasks"] == []


def test_published_work_stays_inside_its_company_and_organization(portal, client):
    api = portal["api"]
    close = approved_close(portal)
    api.portal({"action": "publishTask", "company": portal["company"], "task": close})
    # The other client of the other company sees nothing of it.
    api.role(portal["other_client"])
    assert all(i["title"] != "January 2026 Financial Close" for i in api.portal()["work"])
    api.role("owner")

    services = client.app.state.services
    with services.db.write() as session:
        _org, owner_b = create_organization(session, "firm-publish", "Firm B")
    other_firm = Api(client)
    other_firm.cookie = issue_session(services, owner_b.id)
    other_firm.portal({"action": "companyCreate", "data": {"name": "Firm B Co"}})
    assert "January 2026 Financial Close" not in str(other_firm.portal())
    # Firm B cannot publish firm A's close into its own portal either.
    firm_b_company = next(c["id"] for c in other_firm.portal()["companies"])
    other_firm.portal({"action": "publishTask", "company": firm_b_company, "task": close}, 400)


def test_onboarding_status_reflects_its_own_stages_not_other_work(portal):
    """Completing a close must not imply onboarding progress: they are separate workflows."""
    api = portal["api"]
    close = approved_close(portal)
    api.portal({"action": "publishTask", "company": portal["company"], "task": close})
    api.role(portal["client"])
    work = api.portal()["work"]
    onboarding = next(i for i in work if i["source"] == "onboarding")
    assert onboarding["status"] == tracker.NOT_STARTED  # every stage is still "Not started"
    assert onboarding["progress"] == {"verified": 0, "total": 6}
    assert next(i for i in work if i["title"] == "January 2026 Financial Close")["status"] == tracker.COMPLETED


# Completion detail, timeline and next action ----------------------------------------------------

def test_a_completed_published_item_carries_its_real_completion_date_and_timeline(portal):
    api = portal["api"]
    close = approved_close(portal)
    source = api.get(close)
    api.portal({"action": "publishTask", "company": portal["company"], "task": close,
                "note": "Your January accounts are closed and filed."})
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["title"] == "January 2026 Financial Close")

    assert item["status"] == tracker.COMPLETED
    assert item["completed"] == source["completedAt"]      # the workflow's own timestamp
    assert item["completed"] != item["updated"]            # never the row's last-updated value
    assert item["cancelled"] is None
    assert item["note"] == "Your January accounts are closed and filed."  # the completion summary
    assert item["nextAction"] == tracker.NO_ACTION
    assert [e["key"] for e in item["timeline"]] == ["created", "completedAt"]
    assert [e["label"] for e in item["timeline"]] == ["Work opened", "Completed"]
    assert item["timeline"][0]["at"] == source["created"] < item["timeline"][1]["at"]
    for internal in ("approval", "preparer", "Review variance", "trialbalance", "cancellationReason"):
        assert internal not in str(item), internal


def test_a_cancelled_published_item_shows_the_date_but_never_the_reason(portal):
    api = portal["api"]
    task = api.create("task", {"company": portal["company"], "title": "Abandoned filing",
                               "due": TOMORROW, "assignee": "owner"})
    api.portal({"action": "publishTask", "company": portal["company"], "task": task})
    api.action(task, "transition", {"status": "Cancelled", "reason": "Client withdrew the instruction."})
    source = api.get(task)
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["title"] == "Abandoned filing")
    assert item["status"] == tracker.CANCELLED
    assert item["cancelled"] == source["cancelledAt"] and item["completed"] is None
    assert item["nextAction"] == tracker.NO_ACTION
    assert [e["key"] for e in item["timeline"]] == ["created", "cancelledAt"]
    assert "Client withdrew the instruction." not in str(item)


def test_work_in_flight_has_only_the_events_that_happened(portal):
    api = portal["api"]
    task = api.create("task", {"company": portal["company"], "title": "Ongoing filing",
                               "due": TOMORROW, "assignee": "owner"})
    api.portal({"action": "publishTask", "company": portal["company"], "task": task})
    api.move(task, "In progress")
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["title"] == "Ongoing filing")
    assert [e["key"] for e in item["timeline"]] == ["created"]  # no completion event invented
    assert item["completed"] is None and item["cancelled"] is None
    assert item["nextAction"] == tracker.NO_ACTION


def test_a_request_timeline_uses_the_moments_the_workflow_records(portal):
    api = portal["api"]
    api.role(portal["client"])
    api.action(portal["request"], "respond", {"response": "Statements attached."})
    responded = api.get(portal["request"])
    item = next(i for i in api.portal()["work"] if i["id"] == portal["request"])
    assert item["status"] == tracker.UNDER_REVIEW and item["nextAction"] == tracker.NO_ACTION
    assert [e["key"] for e in item["timeline"]] == ["created", "respondedAt"]
    assert item["timeline"][1]["at"] == responded["respondedAt"]

    api.role("owner")
    api.action(portal["request"], "requestDecision", {"status": "Returned", "reason": "Please add September."})
    api.role(portal["client"])
    returned = next(i for i in api.portal()["work"] if i["id"] == portal["request"])
    assert returned["status"] == tracker.CHANGES_REQUESTED and returned["clientAction"] is True
    assert returned["note"] == "Please add September."
    assert returned["nextAction"] == tracker.REQUEST_NEXT_ACTION
    # Closing a request records no timestamp, so no completion date is claimed.
    api.role("owner")
    api.role(portal["client"])
    api.action(portal["request"], "respond", {"response": "September added."})
    api.role("owner")
    api.action(portal["request"], "requestDecision", {"status": "Closed"})
    api.role(portal["client"])
    closed = next(i for i in api.portal()["work"] if i["id"] == portal["request"])
    assert closed["status"] == tracker.COMPLETED and closed["completed"] is None
    assert [e["key"] for e in closed["timeline"]] == ["created", "respondedAt"]


def test_every_known_status_states_whether_the_client_must_act():
    for stored, expected in (("Not started", tracker.NO_ACTION), ("In progress", tracker.NO_ACTION),
                             ("Under review", tracker.NO_ACTION), ("Completed", tracker.NO_ACTION),
                             ("Cancelled", tracker.NO_ACTION), ("Not applicable", tracker.NO_ACTION),
                             ("Needs scheduling", tracker.NO_ACTION)):
        item = one(state(obligations=[obligation(status=stored)]))
        assert item["nextAction"] == expected and item["clientAction"] is False, stored
    acting = one(state(obligations=[obligation(status="Waiting for client")]))
    assert acting["clientAction"] is True and acting["nextAction"] != tracker.NO_ACTION
    # An unmapped status supports no conclusion either way.
    unknown = one(state(obligations=[obligation(status="Something new")]))
    assert unknown["nextAction"] is None and unknown["clientAction"] is False


def test_onboarding_keeps_its_milestones_as_its_history(portal):
    api = portal["api"]
    api.role(portal["client"])
    item = next(i for i in api.portal()["work"] if i["source"] == "onboarding")
    assert item["timeline"] == []        # the milestones below are the real history
    assert len(item["milestones"]) == 6
    assert item["completed"] is None and item["cancelled"] is None
    assert all(m["updated"] is None for m in item["milestones"])  # nothing verified yet
