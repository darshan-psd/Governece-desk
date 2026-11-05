"""Rule library: lifecycle, independent publication and obligation generation.

Statuses are the existing ones: Draft → Under review → Published (plus Draft again for a returned
rule or a new revision). Every test runs on its own throwaway database with synthetic data.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.jsvalues import today as server_today
from app.repositories.records import RecordRepository

TODAY = date.fromisoformat(server_today())


@pytest.fixture
def desk(api):
    company = api.create("company", {"name": "Rule Test Co"})
    return api, company


def new_rule(api, company, **overrides):
    data = {"company": company, "title": "Monthly compliance review", "due": TODAY.isoformat(), "assignee": "owner",
            "source": "Internal policy 7.2", "frequency": "Monthly", "offset": 0, "notes": "Check registers.", **overrides}
    return api.create("rule", data)


def status(api, rule, target, expect=200, **body):
    return api.action(rule, "ruleStatus", {"status": target, **body}, expect)


def test_lifecycle_records_who_and_when(desk):
    api, company = desk
    rule = new_rule(api, company)
    created = api.get(rule)
    assert created["status"] == "Draft" and created["preparer"] == "owner" and created["ruleVersion"] == 1
    status(api, rule, "Under review")
    submitted = api.get(rule)
    assert submitted["submittedBy"] == "owner" and submitted["submittedAt"]
    status(api, rule, "Published", 400)  # the preparer cannot publish
    api.role("secretary")
    status(api, rule, "Published")
    published = api.get(rule)
    assert published["status"] == "Published" and published["publishedBy"] == "secretary" and published["publishedAt"]
    actions = [a["action"] for a in api.state()["audit"] if a["subject"] == "Monthly compliance review"]
    assert any(a.startswith("Created rule") for a in actions)
    assert any(a.startswith("Submitted rule for review") for a in actions)
    assert any(a.startswith("Published reviewed rule") for a in actions)


def test_returning_a_rule_needs_a_reason_and_never_lets_the_author_self_publish(desk):
    api, company = desk
    rule = new_rule(api, company)
    status(api, rule, "Under review")
    api.role("director")
    status(api, rule, "Draft", 400)  # no reason
    status(api, rule, "Draft", reason="Offset should be 7 days after month end.")
    returned = api.get(rule)
    # Preparer and version are unchanged: the reviewer did not become the author.
    assert returned["status"] == "Draft" and returned["preparer"] == "owner" and returned["ruleVersion"] == 1
    assert returned["reviewNote"] == "Offset should be 7 days after month end." and returned["returnedBy"] == "director"
    api.role("owner")
    status(api, rule, "Under review")
    status(api, rule, "Published", 400)  # previously allowed: the returned rule's author could publish it
    api.role("director")
    status(api, rule, "Published")
    assert api.get(rule)["reviewNote"] is None
    actions = [a["action"] for a in api.state()["audit"] if a["subject"] == "Monthly compliance review"]
    assert any(a.startswith("Returned rule for corrections") for a in actions)


def test_the_preparer_can_withdraw_with_a_reason(desk):
    api, company = desk
    rule = new_rule(api, company)
    status(api, rule, "Under review")
    status(api, rule, "Draft", reason="Wrong source reference.")
    assert api.get(rule)["status"] == "Draft"
    assert any(a["action"].startswith("Withdrew rule from review") for a in api.state()["audit"])


def test_editing_a_rule_under_review_returns_it_to_draft_not_a_dead_end(desk):
    api, company = desk
    rule = new_rule(api, company)
    status(api, rule, "Under review")
    api.edit(rule, {"notes": "Also check the minute book."})
    edited = api.get(rule)
    assert edited["status"] == "Draft"  # was "In progress", from which no transition existed
    status(api, rule, "Under review")
    api.role("secretary")
    status(api, rule, "Published")


def test_a_rule_left_in_progress_by_the_old_edit_defect_can_be_resubmitted(desk, client):
    api, company = desk
    rule = new_rule(api, company)
    record = api.get(rule)
    services = client.app.state.services
    with services.db.write() as session:  # reproduce the legacy state in this throwaway database
        stored = {k: v for k, v in record.items() if k not in ("id", "revision", "updated")}
        assert RecordRepository(session, "local-preview").update_if_revision(rule, record["revision"], {**stored, "status": "In progress"})
    assert api.get(rule)["status"] == "In progress"
    status(api, rule, "Published", 400)
    status(api, rule, "Under review")
    api.role("director")
    status(api, rule, "Published")


def test_new_revision_after_publication(desk):
    api, company = desk
    rule = new_rule(api, company)
    status(api, rule, "Under review")
    api.role("secretary")
    status(api, rule, "Published")
    status(api, rule, "Draft")  # new revision, prepared by the secretary
    revised = api.get(rule)
    assert revised["ruleVersion"] == 2 and revised["preparer"] == "secretary" and revised["publishedBy"] is None


@pytest.mark.parametrize("role", ["accountant", "auditor"])
def test_roles_without_rule_rights(desk, role):
    api, company = desk
    rule = new_rule(api, company)
    api.role(role)
    status(api, rule, "Under review", 403)


# Obligation generation ----------------------------------------------------------------------

def test_only_published_rules_generate_and_never_twice(desk):
    api, company = desk
    rule = new_rule(api, company)
    api.action(rule, "generate", expect=400)
    status(api, rule, "Under review")
    api.action(rule, "generate", expect=400)
    api.role("director")
    status(api, rule, "Published")
    api.action(rule, "generate", expect=403)  # directors may publish and preview, not generate
    api.role("owner")
    plan = api.action(rule, "rulePreview")["plan"]
    horizon = TODAY + timedelta(days=90)
    assert plan and all(TODAY.isoformat() <= p["due"] <= horizon.isoformat() for p in plan)
    assert api.action(rule, "generate")["count"] == len(plan)
    assert api.action(rule, "generate")["count"] == 0
    assert all(p["exists"] for p in api.action(rule, "rulePreview")["plan"])
    tasks = [r for r in api.state()["records"] if r["kind"] == "task" and (r.get("rule") or {}).get("id") == rule]
    assert len(tasks) == len(plan)
    # Each obligation is an ordinary task with its own workflow; publishing completed nothing.
    assert {t["status"] for t in tasks} == {"Not started"} and {t["category"] for t in tasks} == {"Recurring"}
    assert all(t["rule"]["version"] == 1 and t["company"] == company and t["assignee"] == "owner" for t in tasks)
    api.move(tasks[0]["id"], "In progress")
    assert api.get(tasks[0]["id"])["status"] == "In progress" and api.get(rule)["status"] == "Published"


def test_a_past_anchor_date_is_not_overdue_and_past_periods_are_skipped(desk):
    """A rule's date is its first anchor, not a deadline: periods before today are not generated."""
    api, company = desk
    anchor = TODAY - timedelta(days=13)
    rule = new_rule(api, company, due=anchor.isoformat())
    status(api, rule, "Under review")
    api.role("director")
    status(api, rule, "Published")
    plan = api.action(rule, "rulePreview")["plan"]
    assert plan and all(p["due"] >= TODAY.isoformat() for p in plan)
    assert anchor.isoformat() not in {p["anchor"] for p in plan}


def test_offset_moves_each_deadline_after_its_anchor(desk):
    api, company = desk
    rule = new_rule(api, company, offset=7, frequency="Quarterly")
    status(api, rule, "Under review")
    api.role("director")
    status(api, rule, "Published")
    for p in api.action(rule, "rulePreview")["plan"]:
        assert date.fromisoformat(p["due"]) - date.fromisoformat(p["anchor"]) == timedelta(days=7)
