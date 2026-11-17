"""Fixes from the module audit of 2026-11-17: event dates, rule revisions, withdrawn requests and
payment checks (reported by one person, confirmed by another; dates judged in India time)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.portal import billing

D = date.today()
TODAY, YESTERDAY, TOMORROW = D.isoformat(), (D - timedelta(days=1)).isoformat(), (D + timedelta(days=1)).isoformat()


@pytest.fixture
def co(api):
    return api, api.create("company", {"name": "Fix Co"})


def event_tasks(api, event):
    return sorted((r for r in api.state()["records"] if r["kind"] == "task" and r.get("event") == event), key=lambda r: r["id"])


# Corporate events --------------------------------------------------------------------------------

def test_moving_an_event_moves_the_tasks_that_had_its_date(co):
    api, c = co
    event = api.create("event", {"company": c, "title": "Appoint Y", "due": TOMORROW, "assignee": "owner",
                                 "eventType": "Director appointment", "effective": TODAY})
    tasks = event_tasks(api, event)
    own_date = (D + timedelta(days=5)).isoformat()
    api.edit(tasks[0]["id"], {"due": own_date})  # a task given its own date keeps it
    api.move(tasks[1]["id"], "In progress")  # started work moves with the event too
    later = (D + timedelta(days=30)).isoformat()
    api.edit(event, {"due": later})
    dues = {t["id"]: t["due"] for t in event_tasks(api, event)}
    assert dues[tasks[0]["id"]] == own_date
    assert all(due == later for task_id, due in dues.items() if task_id != tasks[0]["id"])
    assert any("Moved 4 event checklist tasks" in e["action"] for e in api.state()["audit"])


def test_cancelling_an_event_still_leaves_its_tasks_to_be_cancelled_one_by_one(co):
    """Deliberate design (see test_cancellation): unchanged by this audit."""
    api, c = co
    event = api.create("event", {"company": c, "title": "Appoint Z", "due": TOMORROW, "assignee": "owner",
                                 "eventType": "Director appointment", "effective": TODAY})
    api.action(event, "transition", {"status": "Cancelled", "reason": "Withdrawn"})
    assert all(t["status"] == "Not started" for t in event_tasks(api, event))


# Rule library ------------------------------------------------------------------------------------

def test_a_new_rule_version_updates_generated_tasks_not_started_yet(co):
    api, c = co
    rule = api.create("rule", {"company": c, "title": "Monthly TDS", "due": TOMORROW, "assignee": "owner", "source": "TDS policy",
                               "frequency": "Monthly", "offset": 0})
    api.action(rule, "ruleStatus", {"status": "Under review"})
    api.role("secretary")
    api.action(rule, "ruleStatus", {"status": "Published"})
    first = api.action(rule, "generate")
    assert first["count"] == 3 and first["updated"] == 0
    tasks = sorted((r for r in api.state()["records"] if (r.get("rule") or {}).get("id") == rule), key=lambda r: r["due"])
    api.role("owner")
    api.move(tasks[0]["id"], "In progress")  # work already started is never changed
    api.role("secretary")
    api.action(rule, "ruleStatus", {"status": "Draft"})
    api.edit(rule, {"offset": 7, "title": "Monthly TDS deposit"})
    api.action(rule, "ruleStatus", {"status": "Under review"})
    api.role("owner")
    api.action(rule, "ruleStatus", {"status": "Published"})  # version 2, published by someone other than its preparer
    preview = api.action(rule, "rulePreview")["plan"]
    assert [p["update"] for p in preview if p["exists"]] == [False, True, True]
    second = api.action(rule, "generate")
    assert second["updated"] == 2
    after = {t["id"]: api.get(t["id"]) for t in tasks}
    started = after[tasks[0]["id"]]
    assert started["rule"]["version"] == 1 and started["title"] == "Monthly TDS"
    for t in tasks[1:]:
        now = after[t["id"]]
        assert now["rule"]["version"] == 2 and now["title"] == "Monthly TDS deposit"
        assert now["due"] == (date.fromisoformat(t["due"]) + timedelta(days=7)).isoformat()
    assert api.action(rule, "generate")["updated"] == 0  # nothing left to bring up to date


# Information requests ----------------------------------------------------------------------------

def test_a_request_can_be_withdrawn_with_a_reason(api):
    company = api.portal({"action": "companyCreate", "clientName": "Req client", "data": {"name": "Req Co"}})["id"]
    client_id = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and company in m["companies"])
    req = api.create("request", {"company": company, "title": "Raised in error", "due": TOMORROW, "assignee": client_id})
    api.action(req, "requestDecision", {"status": "Withdrawn"}, expect=400)  # a reason is required
    api.role("director")
    api.action(req, "requestDecision", {"status": "Withdrawn", "reason": "Duplicate"}, expect=403)
    api.role("owner")
    api.action(req, "requestDecision", {"status": "Withdrawn", "reason": "Raised for the wrong company"})
    record = api.get(req)
    assert record["status"] == "Closed" and record["withdrawn"] is True and record["withdrawReason"] == "Raised for the wrong company"
    api.action(req, "requestDecision", {"status": "Withdrawn", "reason": "again"}, expect=400)
    api.role(client_id)
    api.action(req, "respond", {"response": "too late"}, expect=400)
    api.role("owner")
    assert any(e["action"].startswith("Withdrew request") for e in api.state()["audit"])


def test_a_withdrawn_request_no_longer_blocks_the_financial_close(api):
    company = api.portal({"action": "companyCreate", "clientName": "Close client", "data": {"name": "Close Co"}})["id"]
    client_id = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and company in m["companies"])
    open_requests = [r for r in api.state()["records"] if r["kind"] == "request" and r.get("company") == company and r["status"] != "Closed"]
    for r in open_requests:
        api.action(r["id"], "requestDecision", {"status": "Withdrawn", "reason": "Not needed"})
    api.create("request", {"company": company, "title": "Mistake", "due": TOMORROW, "assignee": client_id})
    mistake = next(r["id"] for r in api.state()["records"] if r["kind"] == "request" and r.get("title") == "Mistake")
    tb = api.trial_balance(company, period="2026-01")
    api.role("accountant")
    api.action(tb, "reviewImport")
    api.role("owner")
    close = api.create("close", {"company": company, "title": "January close", "due": TOMORROW, "assignee": "owner",
                                 "period": "2026-01", "trialbalance": tb, "evidence": [api.upload(company)]})
    api.move(close, "In progress")
    api.move(close, "Under review")
    api.role("secretary")
    assert "information requests" in api.move(close, "Approved", 400)["error"]
    api.role("owner")
    api.action(mistake, "requestDecision", {"status": "Withdrawn", "reason": "Raised by mistake"})
    api.role("secretary")
    api.move(close, "Approved")


# Payments ----------------------------------------------------------------------------------------

def issued_invoice(api, name: str):
    company = api.portal({"action": "companyCreate", "clientName": name + " client", "data": {"name": name, "address": "Road"}})["id"]
    inv = api.portal({"action": "invoiceCreate", "company": company, "data": {"number": name + "-1", "due": TOMORROW,
                      "items": [{"description": "Fee", "quantity": 1, "rate": "100"}], "seller": {"name": "Firm", "address": "Street"}}})["id"]
    api.portal({"action": "invoiceIssue", "company": company, "id": inv, "revision": api.get(inv)["revision"]})
    return company, inv, api.upload(company, content=b"proof", filename="proof.txt", shared="true")


def report(api, company, inv, proof, reference, paid_at=TODAY, expect=200):
    return api.portal({"action": "paymentReport", "company": company, "id": inv, "revision": api.get(inv)["revision"], "amount": "100",
                       "reference": reference, "paidAt": paid_at, "evidence": [proof]}, expect)


def test_a_payment_is_confirmed_by_someone_other_than_who_recorded_it(api):
    company, inv, proof = issued_invoice(api, "Pay Co")
    pay = report(api, company, inv, proof, "UTR1")["id"]
    review = {"action": "paymentReview", "company": company, "id": pay, "status": "Confirmed"}
    error = api.portal({**review, "revision": api.get(pay)["revision"]}, 400)["error"]
    assert error == "A different team member must confirm a payment you recorded."
    api.role("secretary")
    api.portal({**review, "revision": api.get(pay)["revision"]})
    assert api.get(pay)["status"] == "Confirmed"


def test_payment_dates_are_judged_in_india_time(api, monkeypatch):
    company, inv, proof = issued_invoice(api, "Early Co")
    # 20:00 UTC on 17 Nov is 01:30 on 18 Nov in India: 18 Nov is today there, 19 Nov is the future.
    monkeypatch.setattr(billing, "_utc_now", lambda: datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc))
    assert "future" in report(api, company, inv, proof, "UTR-F", paid_at="2026-11-19", expect=400)["error"]
    report(api, company, inv, proof, "UTR-T", paid_at="2026-11-18")
