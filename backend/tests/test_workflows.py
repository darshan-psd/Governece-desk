"""Internal workspace workflows, ported scenario-for-scenario from tests/phases.mjs."""
from datetime import date

import pytest


@pytest.fixture
def workspace(api):
    c = api.create("company", {"name": "Validation company", "jurisdiction": "Internal test"})
    c2 = api.create("company", {"name": "Other company"})
    common = {"company": c, "title": "Validation task", "due": date.today().isoformat(), "assignee": "owner"}
    return api, c, c2, common


def test_preparation_evidence_conflicts_and_independent_approval(workspace):
    api, c, c2, common = workspace
    api.create("task", {**common, "due": "2026-02-30"}, expect=400)
    api.create("task", {**common, "assignee": "missing"}, expect=400)
    document, other_doc = api.upload(c), api.upload(c2)
    api.upload(c, content=b"not pdf", filename="fake.pdf", expect=400)
    api.upload(c, content=b"<script>bad</script>", filename="file.html", expect=400)
    api.create("task", {**common, "evidence": [other_doc]}, expect=400)
    task = api.create("task", {**common, "evidence": [document], "checklist": [{"label": "Prepare", "done": False}], "filing": True})
    api.move(task, "Completed", 400)
    api.move(task, "In progress")
    api.move(task, "Under review", 400)
    stale = api.get(task)
    api.edit(task, {"checklist": [{"label": "Prepare", "done": True}]})
    api.v2({"action": "edit", "id": task, "revision": stale["revision"], "data": {}}, expect=409)  # optimistic locking
    api.move(task, "Under review")
    api.move(task, "Approved", 400)  # preparer cannot approve own work
    api.role("secretary")
    api.move(task, "Approved")
    assert api.get(task)["approval"]["actor"] == "secretary"
    api.move(task, "Completed", 400)  # filing needs an accepted submission
    api.edit(task, {"title": "Locked"}, 400)  # approved records are locked

    # Submissions: authorisation, reference and acknowledgement document before completion.
    submission = api.create("submission", {**common, "title": "Manual filing", "task": task})
    api.action(submission, "submissionStatus", {"status": "Authorised"})
    api.action(submission, "submissionStatus", {"status": "Submitted"}, 400)
    api.action(submission, "submissionStatus", {"status": "Submitted", "reference": "PORTAL-001"})
    api.action(submission, "submissionStatus", {"status": "Accepted", "reference": "ACK-001"}, 400)
    api.action(submission, "submissionStatus", {"status": "Accepted", "reference": "ACK-001", "evidence": [other_doc]}, 400)
    api.action(submission, "submissionStatus", {"status": "Accepted", "reference": "ACK-001", "evidence": [document]})
    api.move(task, "Completed")

    # New document version: superseded evidence, dependent approvals flagged for review.
    api.role("owner")
    second = api.upload(c, previous=document, content=b"New evidence version")
    assert api.get(document)["supersededBy"] == second
    assert api.get(task)["reviewRequired"] is True
    assert api.get(submission)["reviewRequired"] is True
    api.upload(c, previous=document, content=b"Invalid branch", expect=400)
    api.move(task, "In progress")
    api.move(task, "Under review")
    api.role("secretary")
    api.move(task, "Approved", 400)
    api.role("owner")
    api.edit(task, {"evidence": [second]})
    api.move(task, "Under review")
    api.role("secretary")
    api.move(task, "Approved")


def test_dependencies_quorum_meeting_actions_and_packs(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    cycle_a = api.create("task", {**common, "title": "Cycle A"})
    cycle_b = api.create("task", {**common, "title": "Cycle B", "dependencies": [cycle_a]})
    api.edit(cycle_a, {"dependencies": [cycle_b]}, 400)
    blocker = api.create("task", {**common, "title": "Blocker"})
    dependent = api.create("task", {**common, "title": "Dependent work", "evidence": [evidence], "dependencies": [blocker]})
    api.move(dependent, "In progress")
    api.move(dependent, "Under review")
    api.role("secretary")
    api.move(dependent, "Approved", 400)  # dependency not completed
    api.role("owner")

    meeting = api.create("meeting", {**common, "title": "Board decision <script>alert(1)</script>", "quorum": 2, "agenda": "Review quarterly plan",
                                     "minutes": "Resolved to review plan", "evidence": [evidence],
                                     "attendees": [{"name": "Director A", "present": True}, {"name": "Director B", "present": True, "conflict": True}]})
    api.move(meeting, "In progress")
    api.move(meeting, "Under review")
    api.role("director")
    api.move(meeting, "Approved", 400)  # conflicted director excluded from quorum
    api.role("owner")
    api.edit(meeting, {"attendees": [{"name": "Director A", "present": True}, {"name": "Director A", "present": True}]})
    api.move(meeting, "Under review")
    api.role("director")
    api.move(meeting, "Approved", 400)  # duplicate names count once
    api.role("owner")
    api.edit(meeting, {"attendees": [{"name": "Director A", "present": True}, {"name": "Director B", "present": True}]})
    api.move(meeting, "Under review")
    api.role("director")
    api.move(meeting, "Approved")
    api.role("owner")
    follow = api.action(meeting, "meetingAction", {"data": {"title": "Follow up decision", "due": common["due"], "assignee": "secretary"}})
    assert api.get(follow["id"])["meeting"] == meeting

    pack = api.action(meeting, "pack")
    html = api.request("GET", f"/api/v2?export=pack&id={pack['id']}").text
    assert "&lt;script&gt;" in html and "<script>" not in html
    snapshot_revision = api.get(pack["id"])["snapshot"]["record"]["revision"]
    api.move(meeting, "In progress")
    api.edit(meeting, {"minutes": "Changed after snapshot"})
    assert api.get(pack["id"])["snapshot"]["record"]["revision"] == snapshot_revision


def test_corporate_event_sequence_and_completion_gate(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    event = api.create("event", {**common, "title": "Appointment", "eventType": "Director appointment", "effective": common["due"], "evidence": [evidence]})
    assert api.get(event)["status"] == "In progress"
    tasks = [r for r in api.state()["records"] if r.get("event") == event]
    assert len(tasks) == 5
    by_id = {t["id"]: t for t in tasks}
    assert by_id[f"{event}-1"]["dependencies"] == [f"{event}-0"]
    api.move(event, "Under review")
    api.role("secretary")
    api.move(event, "Approved")
    api.move(event, "Completed", 400)  # every event task must be completed first


def test_scoped_client_requests_and_role_enforcement(workspace):
    api, c, c2, common = workspace
    other_doc = api.upload(c2)
    shared = api.upload(c)
    client = api.v2({"action": "member", "name": "Scoped client", "role": "Client", "companies": [c]})["id"]
    auditor = api.v2({"action": "member", "name": "Scoped auditor", "role": "Auditor", "companies": [c2]})["id"]
    request = api.create("request", {**common, "title": "Please supply accounts", "assignee": client})
    api.action(shared, "documentShare", {"shared": True})

    api.role(client)
    client_state = api.state()
    assert all(r["id"] == c or r.get("company") == c for r in client_state["records"])
    assert all(r["kind"] in ("company", "document", "request") for r in client_state["records"])
    assert client_state["audit"] == [] and client_state["members"] == []
    api.create("company", {"name": "Forbidden"}, 403)
    # Spoofed identity headers are ignored; the session decides who is acting.
    assert api.request("POST", "/api/v2", json={"action": "create", "kind": "task", "data": common},
                       headers={"x-governance-actor": "owner", "oai-authenticated-user-id": "spoof"}).status_code == 403
    api.action(request, "respond", {"response": "Please review attached account information"})
    api.action(request, "requestDecision", {"status": "Closed"}, 403)
    assert api.request("GET", f"/api/v2-files?id={other_doc}").status_code == 404
    api.upload(c2, content=b"Denied", expect=403)

    api.role("owner")
    api.action(request, "requestDecision", {"status": "Returned"}, 400)
    api.action(request, "requestDecision", {"status": "Returned", "reason": "Please add supporting figures"})
    api.role(client)
    api.action(request, "respond", {"response": "Added the requested figures"})
    api.role("owner")
    api.action(request, "requestDecision", {"status": "Closed"})

    api.role(auditor)
    assert api.state()["audit"] == []
    api.create("task", {**common, "company": c2}, 403)


def test_trial_balance_import_review_and_close(workspace):
    api, c, _c2, common = workspace
    evidence = api.upload(c)
    api.trial_balance(c, "account,debit,credit\nCash,101,0\nCapital,0,100", 400)
    api.trial_balance(c, "account,debit,credit\nCash,-100,0\nCapital,0,100", 400)
    imported = api.trial_balance(c)
    api.trial_balance(c, expect=400)  # duplicate file
    api.action(imported, "reviewImport", expect=400)  # importer cannot review
    api.role("accountant")
    api.action(imported, "reviewImport")
    api.role("owner")
    close = api.create("close", {**common, "title": "September close", "period": "2026-09", "trialbalance": imported, "evidence": [evidence],
                                 "checklist": [{"label": "Review variance", "done": True}]})
    api.move(close, "In progress")
    api.move(close, "Under review")
    api.role("secretary")
    api.move(close, "Approved")
    api.role("owner")
    newer = api.trial_balance(c, "account,debit,credit\nCash,200,0\nCapital,0,200")
    assert api.get(close)["reviewRequired"] is True
    api.move(close, "Completed", 400)
    api.role("accountant")
    api.action(imported, "reviewImport", expect=400)  # newer version exists
    api.action(newer, "reviewImport")
    api.role("owner")
    api.move(close, "In progress")
    api.edit(close, {"trialbalance": newer})
    api.move(close, "Under review")
    api.role("secretary")
    api.move(close, "Approved")
    api.move(close, "Completed")


def test_close_preparation_persists_and_unlabelled_steps_are_refused(workspace):
    """Notes, checklist and evidence must survive a save and a fresh read of the record, and a
    checklist row added without a label must be refused instead of silently dropped."""
    api, c, c2, common = workspace
    evidence = api.upload(c)
    foreign = api.upload(c2)
    imported = api.trial_balance(c)
    api.role("accountant")
    api.action(imported, "reviewImport")
    api.role("owner")
    close = api.create("close", {**common, "title": "January 2026 Financial Close", "period": "2026-09",
                                 "trialbalance": imported})

    # An unlabelled step is refused at creation and at edit, and changes nothing.
    api.create("close", {**common, "title": "Unlabelled", "period": "2026-09", "trialbalance": imported,
                         "checklist": [{"label": "", "done": True}]}, 400)
    before = api.get(close)
    prepared = {"notes": "Reconcile bank balances and review journal entries.",
                "checklist": [{"id": "step-1", "label": "Reconcile bank", "done": True},
                              {"id": "step-2", "label": "Review journals", "done": False}],
                "evidence": [evidence]}
    for broken in ([{"id": "step-1", "label": "", "done": True}],
                   [{"id": "step-1", "label": "   ", "done": False}],
                   [{"id": "step-1", "label": "Reconcile bank", "done": True}, {"id": "step-2", "label": "", "done": True}]):
        response = api.request("POST", "/api/v2", json={"action": "edit", "id": close, "revision": before["revision"],
                                                        "data": {**prepared, "checklist": broken}})
        assert response.status_code == 400 and response.json()["error"] == "Give every checklist step a label."
    unchanged = api.get(close)
    assert unchanged["revision"] == before["revision"] and unchanged["checklist"] == [] and unchanged["notes"] == ""

    # The valid preparation saves, and reads back identically from both read paths.
    api.edit(close, prepared)
    saved = api.get(close)
    assert saved["notes"] == prepared["notes"] and saved["evidence"] == [evidence]
    assert saved["checklist"] == prepared["checklist"]  # ids and completion states preserved
    page = api.call("/api/v2/records?kind=close")
    assert next(r for r in page["items"] if r["id"] == close)["checklist"] == prepared["checklist"]

    # Another member of the same company reads the same record with the same preparation.
    api.role("secretary")
    reloaded = api.get(close)
    assert reloaded["id"] == close and reloaded["checklist"] == prepared["checklist"]
    assert reloaded["notes"] == prepared["notes"] and reloaded["evidence"] == [evidence]
    api.role("owner")

    # Unrelated rules still hold: evidence must belong to this company, and the review gate still
    # requires every step to be complete.
    api.edit(close, {"evidence": [foreign]}, 400)
    api.move(close, "In progress")
    api.move(close, "Under review", 400)  # "Review journals" is not done
    api.edit(close, {"checklist": [{**prepared["checklist"][0]}, {**prepared["checklist"][1], "done": True}]})
    api.move(close, "Under review")
    assert [s["done"] for s in api.get(close)["checklist"]] == [True, True]


def test_rule_publication_and_idempotent_generation(workspace):
    api, _c, _c2, common = workspace
    rule = api.create("rule", {**common, "title": "Monthly internal review", "source": "Internal board policy 2026", "frequency": "Monthly", "offset": 0})
    api.action(rule, "generate", expect=400)
    api.action(rule, "ruleStatus", {"status": "Under review"})
    api.action(rule, "ruleStatus", {"status": "Published"}, 400)  # preparer cannot publish
    api.role("director")
    api.action(rule, "ruleStatus", {"status": "Published"})
    api.role("owner")
    plan = api.action(rule, "rulePreview")
    assert len(plan["plan"]) >= 2
    assert api.action(rule, "generate")["count"] == len(plan["plan"])
    assert api.action(rule, "generate")["count"] == 0  # duplicates prevented
    original = next(r for r in api.state()["records"] if (r.get("rule") or {}).get("id") == rule)
    api.action(rule, "ruleStatus", {"status": "Draft"})
    assert api.get(rule)["ruleVersion"] == 2
    assert api.get(original["id"])["rule"]["version"] == 1


def test_reminders_briefings_providers_and_exports(workspace):
    api, _c, _c2, common = workspace
    api.create("task", {**common, "title": "Due soon"})
    api.v2({"action": "automation"})
    assert api.v2({"action": "automation"})["count"] == 0  # one reminder per item per day
    briefing = api.v2({"action": "briefing"})
    assert api.get(briefing["id"])["sources"]
    api.v2({"action": "briefing", "ai": True}, 400)  # AI not configured
    notice = next(r for r in api.state()["records"] if r["kind"] == "notification")
    api.action(notice["id"], "dispatchEmail", {"to": "nobody@example.invalid"}, 400)  # email not configured
    api.action(notice["id"], "noticeRead")
    assert api.get(notice["id"])["read"] is True
    api.create("task", {**common, "title": '=HYPERLINK("https://example.invalid")'})
    csv = api.request("GET", "/api/v2?export=csv").text
    assert "'=HYPERLINK" in csv
    assert api.request("GET", "/api/workspace").status_code == 404  # legacy route is not served


def test_audit_trail_records_actor_and_action(workspace):
    api, c, _c2, common = workspace
    api.create("task", {**common, "title": "Audited task"})
    audit = api.state()["audit"]
    entry = next(a for a in audit if a["subject"] == "Audited task")
    assert entry["action"] == "Created task · Workspace owner"
    assert entry["at"].endswith("Z")
