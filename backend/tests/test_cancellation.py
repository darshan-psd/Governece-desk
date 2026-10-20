"""Cancelling work: tasks, meetings, corporate events and financial closes.

The four kinds share one status machine (``records_service.TRANSITIONS``), so cancellation is the
existing ``transition`` action with ``status: "Cancelled"`` plus a reason, exactly as voiding an
invoice records a ``voidReason``. These tests drive it over HTTP for each kind.
"""
from __future__ import annotations

from datetime import date

import pytest

TODAY = date.today().isoformat()
REASON = "Client withdrew the instruction; no filing required."


@pytest.fixture
def workspace(api):
    c = api.create("company", {"name": "Cancellation company"})
    c2 = api.create("company", {"name": "Second company"})
    common = {"company": c, "title": "Cancellable work", "due": TODAY, "assignee": "owner"}
    return api, c, c2, common


def cancel(api, record_id, reason: str = REASON, expect: int = 200):
    return api.action(record_id, "transition", {"status": "Cancelled", "reason": reason}, expect)


def cancel_blind(api, record_id, revision: int, reason: str = REASON, expect: int = 403):
    """Cancellation attempted by an actor that cannot read the record (so cannot look up its
    revision): the request carries the revision taken from an authorised session."""
    return api.v2({"id": record_id, "revision": revision, "action": "transition", "status": "Cancelled", "reason": reason}, expect)


def approvable(api, kind, company, common, title, period: str = "2026-09"):
    """A record of ``kind`` that satisfies its own review gate, ready to be approved.

    ``period`` gives each financial close its own trial-balance import (the same file cannot be
    imported twice for one company, period and currency)."""
    evidence = api.upload(company)
    if kind == "task":
        return api.create("task", {**common, "title": title, "evidence": [evidence],
                                   "checklist": [{"label": "Prepare", "done": True}]})
    if kind == "meeting":
        return api.create("meeting", {**common, "title": title, "evidence": [evidence], "quorum": 1,
                                      "agenda": "Approve the budget", "minutes": "Budget approved",
                                      "attendees": [{"name": "Director A", "present": True}]})
    if kind == "event":
        return api.create("event", {**common, "title": title, "eventType": "Director appointment",
                                    "effective": TODAY, "evidence": [evidence]})
    amount = 100 + int(period[-2:])
    imported = api.trial_balance(company, f"account,debit,credit\nCash,{amount},0\nCapital,0,{amount}", period=period)
    api.role("accountant")
    api.action(imported, "reviewImport")
    api.role("owner")
    return api.create("close", {**common, "title": title, "period": period, "trialbalance": imported,
                                "evidence": [evidence], "checklist": [{"label": "Review variance", "done": True}]})


def drive_to(api, record_id, status):
    """Moves a record to ``status`` along valid transitions from wherever it currently is."""
    current = api.get(record_id)["status"]
    if current == status:
        return
    if current != "In progress":
        api.move(record_id, "In progress")
    if status != "In progress":
        api.move(record_id, status)
    assert api.get(record_id)["status"] == status


def reviewed_close(api, company, common, title="Close to cancel"):
    """A close with a reviewed trial balance, as the close workflow requires."""
    imported = api.trial_balance(company)
    api.role("accountant")
    api.action(imported, "reviewImport")
    api.role("owner")
    return api.create("close", {**common, "title": title, "period": "2026-09", "trialbalance": imported})


# A. Tasks ---------------------------------------------------------------------------------

def test_a_task_can_be_cancelled_from_every_status_before_approval(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    for status in ("Not started", "In progress", "Awaiting information", "Under review"):
        task = api.create("task", {**common, "title": f"Task in {status}", "evidence": [evidence]})
        if status != "Not started":
            api.move(task, "In progress")
        if status == "Awaiting information":
            api.move(task, "Awaiting information")
        if status == "Under review":
            api.move(task, "Under review")
        assert api.get(task)["status"] == status
        cancel(api, task)
        assert api.get(task)["status"] == "Cancelled"


def test_cancellation_records_the_actor_the_time_and_the_reason(workspace):
    api, _c, _c2, common = workspace
    task = api.create("task", {**common, "title": "Reasoned cancellation"})
    cancel(api, task)
    record = api.get(task)
    assert record["cancellationReason"] == REASON
    assert record["cancelledBy"] == "owner" and record["cancelledAt"].endswith("Z")
    # The original record survives: nothing is deleted and the preparation is still readable.
    assert record["title"] == "Reasoned cancellation" and record["due"] == TODAY
    assert record["completedAt"] is None and record.get("approval") is None


def test_a_reason_is_required(workspace):
    api, _c, _c2, common = workspace
    task = api.create("task", {**common, "title": "Needs a reason"})
    cancel(api, task, reason="", expect=400)
    cancel(api, task, reason="   ", expect=400)
    assert api.get(task)["status"] == "Not started"
    response = api.request("POST", "/api/v2", json={"action": "transition", "id": task, "revision": 1, "status": "Cancelled"})
    assert response.status_code == 400 and "reason" in response.json()["error"].lower()


def test_approved_and_completed_work_must_be_reopened_before_cancellation(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    task = api.create("task", {**common, "title": "Approved work", "evidence": [evidence]})
    api.move(task, "In progress")
    api.move(task, "Under review")
    api.role("secretary")
    api.move(task, "Approved")
    cancel(api, task, expect=400)  # an independent approval stands until the work is reopened
    api.move(task, "Completed")
    cancel(api, task, expect=400)
    assert api.get(task)["status"] == "Completed"
    api.move(task, "In progress")  # the existing, audited way back
    cancel(api, task)
    assert api.get(task)["status"] == "Cancelled"


def test_a_cancelled_record_is_terminal_and_read_only(workspace):
    api, _c, _c2, common = workspace
    task = api.create("task", {**common, "title": "Terminal"})
    cancel(api, task)
    cancel(api, task, expect=400)  # no repeated cancellation
    for status in ("In progress", "Under review", "Approved", "Completed", "Not started"):
        api.move(task, status, 400)
    api.edit(task, {"title": "Changed after cancellation"}, 400)
    assert api.get(task)["title"] == "Terminal"
    # History is still readable and commentable.
    api.action(task, "comment", {"content": "Recorded for the file"})
    assert api.get(task)["comments"][0]["content"] == "Recorded for the file"


# B. Meetings ------------------------------------------------------------------------------

def test_a_meeting_can_be_cancelled_and_stops_accepting_follow_ups(workspace):
    api, _c, _c2, common = workspace
    meeting = api.create("meeting", {**common, "title": "Board meeting", "quorum": 1, "agenda": "Quarterly review",
                                     "attendees": [{"name": "Director A", "present": True}]})
    api.move(meeting, "In progress")
    cancel(api, meeting)
    assert api.get(meeting)["status"] == "Cancelled"
    api.action(meeting, "meetingAction", {"data": {"title": "Follow up", "due": TODAY, "assignee": "owner"}}, 400)
    # The minutes, agenda and attendance remain for the record.
    assert api.get(meeting)["agenda"] == "Quarterly review" and api.get(meeting)["attendees"]


# C. Corporate events ----------------------------------------------------------------------

def test_cancelling_an_event_leaves_its_checklist_tasks_alone(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    event = api.create("event", {**common, "title": "Director appointment", "eventType": "Director appointment",
                                 "effective": TODAY, "evidence": [evidence]})
    tasks = [r["id"] for r in api.state()["records"] if r.get("event") == event]
    assert len(tasks) == 5
    cancel(api, event)
    assert api.get(event)["status"] == "Cancelled"
    # No related record is cancelled automatically; each is still live and individually cancellable.
    assert all(api.get(t)["status"] == "Not started" for t in tasks)
    cancel(api, tasks[0])
    assert api.get(tasks[0])["status"] == "Cancelled"


def test_an_event_completes_once_its_tasks_are_completed_or_cancelled(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    event = api.create("event", {**common, "title": "Registered office change", "eventType": "Registered office change",
                                 "effective": TODAY, "evidence": [evidence]})
    tasks = sorted(r["id"] for r in api.state()["records"] if r.get("event") == event)
    api.move(event, "Under review")
    api.role("secretary")
    api.move(event, "Approved")
    api.move(event, "Completed", 400)  # event tasks are still outstanding
    api.role("owner")
    for task in tasks[:-1]:
        cancel(api, task, reason="Superseded by the resolution filed on the same day.")
    api.move(event, "Completed", 400)  # one task is neither completed nor cancelled
    cancel(api, tasks[-1], reason="Superseded by the resolution filed on the same day.")
    api.role("secretary")
    api.move(event, "Completed")
    assert api.get(event)["status"] == "Completed"


# D. Financial close -----------------------------------------------------------------------

def test_a_financial_close_can_be_cancelled_without_closing_the_period(workspace):
    api, c, _c2, common = workspace
    close = reviewed_close(api, c, common)
    api.move(close, "In progress")
    cancel(api, close, reason="Period reopened by the auditor; close abandoned.")
    record = api.get(close)
    assert record["status"] == "Cancelled" and record["completedAt"] is None
    assert record["period"] == "2026-09" and record["trialbalance"]
    # A new close for the same period can still be prepared on the same trial balance.
    second = api.create("close", {**common, "title": "September close, second attempt", "period": "2026-09",
                                  "trialbalance": record["trialbalance"]})
    assert api.get(second)["status"] == "Not started"


# Permissions and tenancy -------------------------------------------------------------------

def test_only_authorised_roles_may_cancel(workspace):
    api, c, _c2, common = workspace
    task = api.create("task", {**common, "title": "Role guarded"})
    meeting = api.create("meeting", {**common, "title": "Role guarded meeting", "quorum": 1})
    client = api.v2({"action": "member", "name": "Portal client", "role": "Client", "companies": [c]})["id"]
    revisions = {task: api.get(task)["revision"], meeting: api.get(meeting)["revision"]}
    for role in ("director", "auditor"):  # review-only and read-only roles cannot cancel
        api.role(role)
        cancel(api, task, expect=403)
        cancel(api, meeting, expect=403)
    api.role(client)
    cancel_blind(api, task, revisions[task], expect=404)  # a client cannot even see internal work
    api.role("owner")
    assert api.get(task)["status"] == "Not started"
    # Every editing role may cancel.
    for role, record in (("secretary", task), ("accountant", meeting)):
        api.role(role)
        cancel(api, record)
    api.role("owner")
    assert api.get(task)["status"] == "Cancelled" and api.get(meeting)["status"] == "Cancelled"


def test_a_scoped_member_cannot_cancel_work_of_another_company(workspace):
    api, c, c2, common = workspace
    other = api.create("task", {**common, "company": c2, "title": "Other company work"})
    scoped = api.v2({"action": "member", "name": "Scoped secretary", "role": "Secretary", "companies": [c]})["id"]
    revision = api.get(other)["revision"]
    api.role(scoped)
    cancel_blind(api, other, revision, expect=404)
    api.role("owner")
    assert api.get(other)["status"] == "Not started"


def test_another_organization_cannot_cancel_this_organizations_work(client, api):
    from app.auth import issue_session
    from app.services.identity_service import create_organization
    from tests.conftest import Api

    company = api.create("company", {"name": "Firm A company"})
    task = api.create("task", {"company": company, "title": "Firm A task", "due": TODAY, "assignee": "owner"})
    services = client.app.state.services
    with services.db.write() as session:
        _, owner_b = create_organization(session, "firm-cancel", "Firm B")
    other = Api(client)
    other.cookie = issue_session(services, owner_b.id)
    response = other.request("POST", "/api/v2", json={"action": "transition", "id": task, "revision": 1,
                                                      "status": "Cancelled", "reason": REASON})
    assert response.status_code == 404
    assert api.get(task)["status"] == "Not started"


# Audit, optimistic locking and API validation ---------------------------------------------

def test_cancellation_is_audited_with_the_actor_and_the_record(workspace):
    api, _c, _c2, common = workspace
    task = api.create("task", {**common, "title": "Audited cancellation"})
    api.role("secretary")
    cancel(api, task)
    api.role("owner")
    entry = next(a for a in api.state()["audit"] if a["subject"] == "Audited cancellation" and "Cancelled" in a["action"])
    assert entry["action"] == "Status: Cancelled · Priya Sharma"
    assert entry["at"].endswith("Z")
    # The creation entry is still there: the trail is appended to, never rewritten.
    assert any(a["action"].startswith("Created task") and a["subject"] == "Audited cancellation" for a in api.state()["audit"])


def test_cancellation_uses_the_existing_optimistic_locking_and_error_shape(workspace):
    api, _c, _c2, common = workspace
    task = api.create("task", {**common, "title": "Locked cancellation"})
    stale = api.get(task)["revision"]
    api.edit(task, {"notes": "Added a note"})
    conflict = api.v2({"action": "transition", "id": task, "revision": stale, "status": "Cancelled", "reason": REASON}, 409)
    assert "error" in conflict
    assert api.get(task)["status"] == "Not started"
    assert "error" in api.v2({"action": "transition", "id": "no-such-record", "revision": 1,
                              "status": "Cancelled", "reason": REASON}, 404)


# Dashboards, filters and reminders ---------------------------------------------------------

def test_cancelled_work_is_not_outstanding_for_reminders_briefings_or_filters(workspace):
    api, _c, _c2, common = workspace
    keep = api.create("task", {**common, "title": "Still due"})
    drop = api.create("task", {**common, "title": "Cancelled and due"})
    cancel(api, drop)

    # Reminders: one notice for the live task only.
    assert api.v2({"action": "automation"})["count"] == 1
    notices = [r for r in api.state()["records"] if r["kind"] == "notification"]
    assert [n["task"] for n in notices] == [keep]

    # Briefing: the cancelled task is not a source and not counted as open.
    briefing = api.v2({"action": "briefing"})["id"]
    sources = api.get(briefing)["sources"]
    assert [s["id"] for s in sources] == [keep]
    assert "1 open items" in api.get(briefing)["content"]

    # Record list filters keep working on the new status, and the record is still retrievable.
    cancelled = api.call(f"/api/v2/records?kind=task&status=Cancelled")
    assert [r["id"] for r in cancelled["items"]] == [drop]
    live = api.call("/api/v2/records?kind=task&status=Not started")
    assert [r["id"] for r in live["items"]] == [keep]
    # Historical reporting keeps cancelled work visible rather than silently dropping it.
    csv = api.request("GET", "/api/v2?export=csv").text
    assert "Cancelled and due" in csv and "Cancelled" in csv


def test_cancelled_work_leaves_the_client_portal_calendar(workspace):
    api, c, _c2, common = workspace
    api.portal({"action": "companySave", "id": c, "revision": api.get(c)["revision"],
                "data": {"name": "Cancellation company", "address": "1 Road"}})
    task = api.create("task", {**common, "title": "Published obligation"})
    api.portal({"action": "publishTask", "company": c, "task": task})
    assert any(o["title"] == "Published obligation" for o in api.portal()["obligations"])
    assert any(d["title"] == "Published obligation" for d in api.portal()["calendar"])

    cancel(api, task)
    state = api.portal()
    obligation = next(o for o in state["obligations"] if o["title"] == "Published obligation")
    assert obligation["status"] == "Cancelled"  # the published item mirrors the internal task
    assert not any(d["title"] == "Published obligation" for d in state["calendar"])
    assert not any(d["title"] == "Published obligation" for d in state["alerts"])
    # A cancelled task can no longer be published, and is not offered for publishing.
    assert not any(t["id"] == task for t in state["internalTasks"])
    second = api.create("task", {**common, "title": "Another obligation"})
    cancel(api, second)
    api.portal({"action": "publishTask", "company": c, "task": second}, 400)


# Regression: the existing lifecycle is unchanged ------------------------------------------

def test_the_existing_workflow_still_runs_end_to_end(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    task = api.create("task", {**common, "title": "Unaffected work", "evidence": [evidence],
                               "checklist": [{"label": "Prepare", "done": True}]})
    api.move(task, "In progress")
    api.move(task, "Awaiting information")
    api.move(task, "In progress")
    api.move(task, "Under review")
    api.role("secretary")
    api.move(task, "Approved")
    api.move(task, "Completed")
    assert api.get(task)["status"] == "Completed" and api.get(task)["completedAt"]
    api.move(task, "In progress")
    assert api.get(task)["approval"] is None


# Regression: the reported "budget" meeting ------------------------------------------------

def test_a_meeting_under_review_is_cancelled_from_the_calendar(workspace):
    """Reported failure: the meeting "budget" (kind ``meeting``, status ``Under review``, opened
    from the Calendar module) answered "This workflow transition is not allowed." when cancelled
    with the reason "test". ``Under review`` is a pre-approval status, so the request body the
    dialog sends must be accepted."""
    api, c, _c2, common = workspace
    meeting = api.create("meeting", {**common, "title": "budget", "quorum": 1, "agenda": "Annual budget",
                                     "due": "2026-10-14"})
    # Several preparation edits first: the record is at a later revision, as the reported one was.
    for note in ("First draft", "Second draft", "Third draft"):
        api.edit(meeting, {"notes": note})
    api.move(meeting, "In progress")
    api.move(meeting, "Under review")
    record = api.get(meeting)
    assert record["kind"] == "meeting" and record["status"] == "Under review" and record["revision"] > 1

    # Exactly the body the confirmation dialog posts.
    result = api.v2({"action": "transition", "id": meeting, "revision": record["revision"],
                     "status": "Cancelled", "reason": "test"})
    assert result["ok"] is True
    cancelled = api.get(meeting)
    assert cancelled["status"] == "Cancelled" and cancelled["cancellationReason"] == "test"
    assert cancelled["cancelledBy"] == "owner" and cancelled["cancelledAt"].endswith("Z")
    assert cancelled["title"] == "budget" and cancelled["agenda"] == "Annual budget"
    assert any(a["subject"] == "budget" and a["action"] == "Status: Cancelled · Workspace owner"
               for a in api.state()["audit"])


@pytest.mark.parametrize("kind", ("task", "meeting", "close", "event"))
def test_cancelled_is_allowed_from_every_pre_approval_status_of_every_kind(api, kind):
    """The four modules share one status machine: cancellation is offered from every status up to
    and including review, for each of them."""
    from app.services.records_service import TRANSITIONS

    assert all("Cancelled" in TRANSITIONS[status]
               for status in ("Not started", "In progress", "Awaiting information", "Under review"))
    assert TRANSITIONS["Cancelled"] == [] and "Cancelled" not in TRANSITIONS["Approved"] + TRANSITIONS["Completed"]

    c = api.create("company", {"name": f"Matrix company {kind}"})
    common = {"company": c, "title": f"Matrix {kind}", "due": TODAY, "assignee": "owner"}
    # A corporate event starts "In progress"; every other kind starts "Not started".
    statuses = ["In progress", "Awaiting information", "Under review"]
    if kind != "event":
        statuses.insert(0, "Not started")
    for index, status in enumerate(statuses):
        record = approvable(api, kind, c, {**common, "title": f"Matrix {kind} {index}"}, f"Matrix {kind} {index}",
                            period=f"2026-0{index + 5}")
        drive_to(api, record, status)
        cancel(api, record, reason=f"Cancelled from {status}.")
        assert api.get(record)["status"] == "Cancelled", status


@pytest.mark.parametrize("kind", ("task", "meeting", "close", "event"))
def test_approved_work_of_every_kind_still_refuses_cancellation(api, kind):
    c = api.create("company", {"name": f"Approval company {kind}"})
    common = {"company": c, "title": f"Approved {kind}", "due": TODAY, "assignee": "owner"}
    record = approvable(api, kind, c, common, f"Approved {kind}")
    drive_to(api, record, "Under review")
    api.role("secretary")
    api.move(record, "Approved")
    cancel(api, record, expect=400)
    assert api.get(record)["status"] == "Approved"
    api.role("owner")
