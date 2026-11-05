"""Bulk compliance calendar setup with reusable templates (``app.services.calendar_service``).

Every test runs on its own throwaway database (SQLite by default; ``GOVERNANCE_TEST_DATABASE=postgres``
runs the same tests on a temporary PostgreSQL cluster) with synthetic companies and the local preview
profiles only. Nothing here reads or writes a configured database.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.jsvalues import today as server_today
from app.repositories.records import RecordRepository

TODAY = date.fromisoformat(server_today())
START = TODAY.year if TODAY.month >= 4 else TODAY.year - 1
FY_NOW = f"{START}-{(START + 1) % 100:02d}"
FY = f"{START + 1}-{(START + 2) % 100:02d}"  # the next financial year: every period is still ahead
Y = START + 1  # its first calendar year (April to December)

ACTIVITIES = [
    {"name": "GST Return", "category": "GST", "frequency": "Monthly", "dueDay": 11, "monthsAfter": 1, "role": "Secretary",
     "reminders": {"days": [7, 3, 1], "overdue": True}, "instructions": "Reconcile outward supplies before filing.",
     "dateBasis": "Synthetic basis: verify for each period", "clientVisible": True},
    {"name": "TDS Return", "category": "TDS", "frequency": "Quarterly", "dueDay": 31, "monthsAfter": 1, "role": "Accountant"},
    {"name": "Annual Compliance Review", "category": "Annual compliance", "frequency": "Annual", "dueDay": 30, "monthsAfter": 6,
     "role": "Secretary", "verifyDates": False},
    {"name": "Annual Filing", "category": "Annual filing", "frequency": "Annual", "dueDay": 30, "monthsAfter": 7, "role": "Secretary",
     "filing": True},
]
PER_COMPANY = 12 + 4 + 1 + 1
FIELDS = ("name", "description", "companyType", "financialYear", "activities")


@pytest.fixture
def desk(api):
    names = ["Zoho Synthetic", "ABC Synthetic Pvt Ltd", "XYZ Synthetic Pvt Ltd", "PS Synthetic Digitise"]
    return api, [api.create("company", {"name": n, "entityType": "Private limited company"}) for n in names]


def make_template(api, **overrides) -> str:
    data = {"name": "Standard Private Limited Company", "description": "Synthetic test template",
            "companyType": "Private limited company", "financialYear": FY, "activities": ACTIVITIES, **overrides}
    return api.v2({"action": "templateCreate", "data": data})["id"]


def save_template(api, template_id, expect=200, **changes):
    record = api.get(template_id)
    return api.action(template_id, "templateSave", {"data": {**{k: record[k] for k in FIELDS}, **changes}}, expect)


def activity_id(api, template_id, name) -> str:
    return next(a["id"] for a in api.get(template_id)["activities"] if a["name"] == name)


def preview(api, template_id, companies, expect=200, **body):
    return api.v2({"action": "calendarPreview", "template": template_id, "companies": companies, "financialYear": FY, **body}, expect)


def apply(api, template_id, companies, expect=200, **body):
    version = api.get(template_id)["templateVersion"]
    return api.v2({"action": "calendarApply", "template": template_id, "templateVersion": version, "companies": companies,
                   "financialYear": FY, **body}, expect)


def entries(api, company=None) -> list[dict]:
    return [r for r in api.state()["records"] if r["kind"] == "task" and isinstance(r.get("template"), dict)
            and (company is None or r["company"] == company)]


def actions(api) -> list[str]:
    return [a["action"] for a in api.state()["audit"]]


# Templates ------------------------------------------------------------------------------------

def test_templates_are_versioned_and_never_create_entries_on_their_own(desk):
    api, _ = desk
    t = make_template(api)
    record = api.get(t)
    assert record["kind"] == "calendartemplate" and record["status"] == "Active" and record["templateVersion"] == 1
    assert [a["name"] for a in record["activities"]] == [a["name"] for a in ACTIVITIES]
    gst, tds, review = record["activities"][:3]
    assert gst["reminders"] == {"days": [7, 3, 1], "overdue": True} and gst["verifyDates"] is True and gst["clientVisible"] is True
    assert tds["reminders"] == {"days": [7, 3, 1], "overdue": True} and tds["clientVisible"] is False  # defaults
    assert review["verifyDates"] is False
    assert entries(api) == []
    # A new version: rename and change one activity, drop one, add one. Kept activities keep their ids.
    edited = [{**gst, "name": "GST Return (GSTR-3B)", "dueDay": 20}, tds, record["activities"][3],
              {"name": "Board Meeting", "category": "Board", "frequency": "Quarterly", "dueDay": 15, "monthsAfter": 0, "role": "Secretary"}]
    save_template(api, t, activities=edited)
    updated = api.get(t)
    assert updated["templateVersion"] == 2
    ids = [a["id"] for a in updated["activities"]]
    assert ids[:3] == [gst["id"], tds["id"], record["activities"][3]["id"]]
    assert ids[3] not in {a["id"] for a in record["activities"]}
    assert updated["activities"][0]["name"] == "GST Return (GSTR-3B)" and updated["activities"][0]["dueDay"] == 20
    assert entries(api) == []
    log = actions(api)
    assert any(a.startswith("Created calendar template") for a in log)
    assert any(a.startswith("Updated calendar template (version 2)") for a in log)


@pytest.mark.parametrize("change, message", [
    ({"name": ""}, "name"),
    ({"activities": []}, "at least one"),
    ({"financialYear": "2026-28"}, "financial year"),
    ({"companyType": "Partnership"}, "company type"),
    ({"activities": [{**ACTIVITIES[0], "frequency": "Weekly"}]}, "recurs"),
    ({"activities": [{**ACTIVITIES[0], "dueDay": 32}]}, "due day"),
    ({"activities": [{**ACTIVITIES[0], "dueDay": True}]}, "due day"),
    ({"activities": [{**ACTIVITIES[0], "monthsAfter": 13}]}, "months after"),
    ({"activities": [ACTIVITIES[0], {**ACTIVITIES[1], "name": "gst  return"}]}, "listed twice"),
    ({"activities": [{**ACTIVITIES[0], "role": "Client"}]}, "responsible role"),
])
def test_template_validation(desk, change, message):
    api, _ = desk
    data = {"name": "Template", "financialYear": FY, "activities": ACTIVITIES, **change}
    assert message in api.v2({"action": "templateCreate", "data": data}, 400)["error"].lower()


@pytest.mark.parametrize("role", ["secretary", "accountant", "director", "auditor"])
def test_only_the_owner_manages_templates(desk, role):
    api, _ = desk
    t = make_template(api)
    api.role(role)
    api.v2({"action": "templateCreate", "data": {"name": "Mine", "financialYear": FY, "activities": ACTIVITIES}}, 403)
    save_template(api, t, expect=403, name="Changed")
    api.action(t, "templateDuplicate", expect=403)
    api.action(t, "templateArchive", expect=403)
    assert api.get(t)["templateVersion"] == 1  # staff with access to every company may still read it


def test_duplicate_archive_and_restore(desk):
    api, companies = desk
    t = make_template(api, secretaryApply=True)
    copy = api.get(api.action(t, "templateDuplicate")["id"])
    assert copy["name"] == "Standard Private Limited Company (copy)" and copy["templateVersion"] == 1
    assert copy["secretaryApply"] is False and copy["duplicatedFrom"]["id"] == t
    assert [a["name"] for a in copy["activities"]] == [a["name"] for a in ACTIVITIES]
    api.action(t, "templateArchive", {"archived": True})
    assert api.get(t)["status"] == "Archived"
    assert "Restore" in preview(api, t, companies[:1], 400)["error"]
    save_template(api, t, expect=400, name="Edited while archived")
    api.action(t, "templateArchive", {"archived": True}, 400)
    api.action(t, "templateArchive", {"archived": False})
    assert api.get(t)["status"] == "Active"
    log = actions(api)
    for action in ("Duplicated calendar template", "Archived calendar template", "Restored calendar template"):
        assert any(a.startswith(action) for a in log), action


# Preview --------------------------------------------------------------------------------------

def test_preview_plans_every_company_and_writes_nothing(desk):
    api, companies = desk
    t = make_template(api)
    before = api.state()["records"]
    plan = preview(api, t, companies)
    s = plan["summary"]
    assert s["companies"] == s["eligibleCompanies"] == 4 and s["activities"] == 4 and s["template"] == "Standard Private Limited Company"
    assert s["entries"] == s["new"] == 4 * PER_COMPANY and s["existing"] == s["duplicates"] == 0
    assert s["needsConfirmation"] == 4 * (PER_COMPANY - 1)  # the annual review is an internal deadline
    first = plan["companies"][0]
    due = {(e["title"], e["period"]): e["due"] for e in first["entries"]}
    assert due[("GST Return", f"{Y}-04")] == f"{Y}-05-11"
    assert due[("GST Return", f"{Y}-10")] == f"{Y}-11-11"
    assert due[("GST Return", f"{Y + 1}-03")] == f"{Y + 1}-04-11"
    assert due[("TDS Return", f"{FY}-Q1")] == f"{Y}-07-31"
    assert due[("TDS Return", f"{FY}-Q4")] == f"{Y + 1}-04-30"  # day 31 is clamped to the month's last day
    assert due[("Annual Compliance Review", FY)] == f"{Y + 1}-09-30"
    assert due[("Annual Filing", FY)] == f"{Y + 1}-10-30"
    assert first["assignments"] == {"Secretary": "secretary", "Accountant": "accountant"}
    gst = next(e for e in first["entries"] if e["title"] == "GST Return")
    assert gst["assignee"] == "secretary" and gst["confirmed"] is False and gst["verify"] is True
    assert api.state()["records"] == before


def test_periods_already_due_are_left_out_unless_included(desk):
    api, companies = desk
    t = make_template(api, financialYear=FY_NOW)
    body = {"action": "calendarPreview", "template": t, "companies": companies[:1], "financialYear": FY_NOW}
    plan = api.v2(body)
    past = [e for e in plan["companies"][0]["entries"] if e["due"] < TODAY.isoformat()]
    assert all(e["status"] == "past" for e in past) and plan["summary"]["past"] == len(past)
    assert plan["summary"]["new"] == PER_COMPANY - len(past)
    included = api.v2({**body, "includePast": True})["summary"]
    assert included["past"] == 0 and included["new"] == PER_COMPANY


def test_only_active_companies_of_the_template_type_are_eligible(desk):
    api, companies = desk
    zoho = companies[0]
    section8 = api.create("company", {"name": "Charity Synthetic", "entityType": "Section 8 company"})
    untyped = api.create("company", {"name": "Untyped Synthetic"})
    december = api.create("company", {"name": "December Synthetic", "entityType": "Private limited company", "yearEnd": "31 December"})
    api.action(zoho, "companyStatus", {"inactive": True}, 400)  # a reason is required
    api.action(zoho, "companyStatus", {"inactive": True, "reason": "Engagement ended"})
    t = make_template(api)
    plan = {c["id"]: c for c in preview(api, t, [zoho, section8, untyped, december])["companies"]}
    assert plan[zoho]["eligible"] is False and plan[zoho]["reason"] == "Inactive company" and plan[zoho]["entries"] == []
    assert plan[section8]["eligible"] is False and "Section 8 company" in plan[section8]["reason"]
    assert plan[untyped]["eligible"] is True and any("type not recorded" in w for w in plan[untyped]["warnings"])
    assert plan[december]["eligible"] is True and any("31 December" in w for w in plan[december]["warnings"])
    results = {r["company"]: r for r in apply(api, t, [zoho, section8, untyped])["results"]}
    assert results[zoho]["status"] == results[section8]["status"] == "Failed"
    assert results[untyped]["status"] == "Completed" and results[untyped]["created"] == PER_COMPANY
    assert entries(api, zoho) == [] and entries(api, section8) == []
    api.role("secretary")
    api.action(zoho, "companyStatus", {"inactive": False}, 403)  # only the Owner changes a company's status
    api.role("owner")
    api.action(zoho, "companyStatus", {"inactive": False})
    assert preview(api, t, [zoho])["companies"][0]["eligible"] is True
    assert any(a.startswith("Marked company inactive") for a in actions(api))


def test_invalid_company_dates_are_refused(desk):
    api, companies = desk
    t = make_template(api)
    gst = activity_id(api, t, "GST Return")
    assert "valid due date" in preview(api, t, companies[:1], 400, dates={companies[0]: {f"{gst}:{Y}-10": f"{Y + 1}-02-30"}})["error"]


# Bulk creation --------------------------------------------------------------------------------

def test_bulk_creation_independent_calendars_and_duplicate_prevention(desk):
    api, companies = desk
    zoho, abc, xyz, ps = companies
    t = make_template(api)
    gst, tds = activity_id(api, t, "GST Return"), activity_id(api, t, "TDS Return")
    october, first_quarter = f"{gst}:{Y}-10", f"{tds}:{FY}-Q1"
    config = {"dates": {zoho: {october: f"{Y}-11-15"}}, "confirmed": {zoho: [october]}, "excluded": {abc: [first_quarter]}}
    plan = {c["id"]: c for c in preview(api, t, companies, **config)["companies"]}
    adjusted = next(e for e in plan[zoho]["entries"] if e["key"] == october)
    assert adjusted["due"] == f"{Y}-11-15" and adjusted["generatedDue"] == f"{Y}-11-11" and adjusted["adjusted"] and adjusted["confirmed"]
    assert next(e for e in plan[abc]["entries"] if e["key"] == october)["due"] == f"{Y}-11-11"
    assert next(e for e in plan[abc]["entries"] if e["key"] == first_quarter)["status"] == "excluded"

    # One request per company, as the page sends them to show progress, all in one run.
    run = "run-" + "1" * 12
    results = [apply(api, t, [c], selection=companies, run=run, **config)["results"][0] for c in companies]
    assert [r["status"] for r in results] == ["Completed"] * 4
    assert [r["created"] for r in results] == [PER_COMPANY, PER_COMPANY - 1, PER_COMPANY, PER_COMPANY]

    zoho_oct, abc_oct = api.get(f"tpl-{t}-{zoho}-{gst}-{Y}-10"), api.get(f"tpl-{t}-{abc}-{gst}-{Y}-10")
    assert zoho_oct["company"] == zoho and abc_oct["company"] == abc
    assert zoho_oct["due"] == f"{Y}-11-15" and abc_oct["due"] == f"{Y}-11-11"
    origin = zoho_oct["template"]
    assert origin["id"] == t and origin["version"] == 1 and origin["batch"] == f"calbatch-{run}" and origin["createdBy"] == "owner"
    assert origin["createdAt"] and origin["dateConfirmed"] is True and origin["confirmedBy"] == "owner" and origin["adjusted"] is True
    assert abc_oct["template"]["dateConfirmed"] is False and abc_oct["template"]["adjusted"] is False
    assert zoho_oct["status"] == "Not started" and zoho_oct["assignee"] == "secretary" and zoho_oct["category"] == "GST"
    assert zoho_oct["preparer"] == "owner" and zoho_oct["reminders"] == {"days": [7, 3, 1], "overdue": True}
    assert zoho_oct["period"] == f"Oct {Y}" and zoho_oct["notes"] == ACTIVITIES[0]["instructions"]
    assert api.get(f"tpl-{t}-{abc}-{tds}-{FY}-Q1") is None and api.get(f"tpl-{t}-{abc}-{tds}-{FY}-Q2")["assignee"] == "accountant"

    batch = api.get(f"calbatch-{run}")
    assert batch["status"] == "Completed" and batch["initiatedBy"] == "owner" and batch["selection"] == companies
    assert sum(r["createdTotal"] for r in batch["results"].values()) == 4 * PER_COMPANY - 1
    log = actions(api)
    assert sum(a.startswith("Started bulk calendar setup") for a in log) == 1
    assert sum(a.startswith("Bulk calendar setup: created") for a in log) == 4

    # Independent calendars: moving Zoho's deadline changes nothing anywhere else.
    api.edit(zoho_oct["id"], {"due": f"{Y}-11-20"})
    assert api.get(zoho_oct["id"])["due"] == f"{Y}-11-20"
    assert api.get(abc_oct["id"])["due"] == f"{Y}-11-11" and api.get(f"tpl-{t}-{xyz}-{gst}-{Y}-10")["due"] == f"{Y}-11-11"
    assert api.get(t)["templateVersion"] == 1

    # The same template again creates nothing.
    again = preview(api, t, companies, **config)["summary"]
    assert again["new"] == 0 and again["existing"] == 4 * PER_COMPANY - 1
    assert all(r["created"] == 0 for r in apply(api, t, companies, **config)["results"])
    # A new template version never rewrites entries generated from the old one.
    record = api.get(t)
    save_template(api, t, activities=[{**record["activities"][0], "dueDay": 25}, *record["activities"][1:]])
    assert api.get(zoho_oct["id"])["due"] == f"{Y}-11-20" and api.get(abc_oct["id"])["due"] == f"{Y}-11-11"
    assert preview(api, t, companies)["summary"]["new"] == 1  # only ABC's excluded quarter is still missing
    # A copy of the template recognises the equivalent obligations and skips them.
    copy = api.action(t, "templateDuplicate")["id"]
    plan = preview(api, copy, [zoho, xyz])["summary"]
    assert plan["new"] == 0 and plan["duplicates"] == 2 * PER_COMPANY
    assert len(entries(api)) == 4 * PER_COMPANY - 1


def test_apply_refuses_a_preview_of_an_older_template_version(desk):
    api, companies = desk
    t = make_template(api)
    save_template(api, t, description="Changed after the preview")
    body = {"action": "calendarApply", "template": t, "templateVersion": 1, "companies": companies[:1], "financialYear": FY}
    assert "changed after your preview" in api.v2(body, 409)["error"]
    assert entries(api) == []


def test_partial_failure_is_reported_and_a_retry_never_duplicates(desk):
    api, companies = desk
    zoho, abc, xyz = companies[:3]
    t = make_template(api)
    scoped = api.v2({"action": "member", "name": "Zoho-only Secretary", "role": "Secretary", "companies": [zoho]})["id"]
    wrong = {"assignments": {xyz: {"Secretary": scoped}}}
    assert {c["id"]: c for c in preview(api, t, [zoho, abc, xyz], **wrong)["companies"]}[xyz]["assignmentErrors"]
    run = "retry-" + "2" * 10
    first = {r["company"]: r for r in apply(api, t, [zoho, abc, xyz], run=run, **wrong)["results"]}
    assert first[zoho]["status"] == first[abc]["status"] == "Completed" and first[xyz]["status"] == "Failed"
    assert "can no longer be assigned" in first[xyz]["error"]
    assert api.get(f"calbatch-{run}")["status"] == "Partially failed"
    assert entries(api, xyz) == [] and len(entries(api, zoho)) == PER_COMPANY
    assert any(a.startswith("Bulk calendar setup failed") for a in actions(api))
    # Retrying the whole selection with the assignment corrected only fills in the failed company.
    second = {r["company"]: r for r in apply(api, t, [zoho, abc, xyz], run=run)["results"]}
    assert second[xyz]["created"] == PER_COMPANY and second[zoho]["created"] == second[abc]["created"] == 0
    assert second[zoho]["existing"] == PER_COMPANY
    batch = api.get(f"calbatch-{run}")
    assert batch["status"] == "Completed" and batch["results"][xyz]["attempts"] == 2
    assert all(len(entries(api, c)) == PER_COMPANY for c in (zoho, abc, xyz))


def test_a_storage_failure_rolls_back_only_that_company(desk, monkeypatch):
    api, companies = desk
    zoho, abc = companies[:2]
    t = make_template(api)
    original, calls = RecordRepository.insert_or_ignore, {"n": 0}

    def failing(self, record_id, kind, data):
        if kind == "task" and data.get("company") == abc:
            calls["n"] += 1
            if calls["n"] == 5:  # part-way through the company's entries
                raise RuntimeError("simulated storage failure")
        return original(self, record_id, kind, data)

    monkeypatch.setattr(RecordRepository, "insert_or_ignore", failing)
    results = {r["company"]: r for r in apply(api, t, [zoho, abc])["results"]}
    monkeypatch.setattr(RecordRepository, "insert_or_ignore", original)
    assert results[zoho]["status"] == "Completed" and results[abc]["status"] == "Failed"
    assert "Nothing was created" in results[abc]["error"]
    assert entries(api, abc) == []  # the company's transaction was rolled back: no half calendar
    retry = {r["company"]: r for r in apply(api, t, [zoho, abc])["results"]}
    assert retry[abc]["created"] == PER_COMPANY and retry[zoho]["created"] == 0


# Rule Library ---------------------------------------------------------------------------------

def test_published_rules_stay_the_source_of_their_obligations(desk):
    api, companies = desk
    zoho, abc, _, ps = companies
    rule = api.create("rule", {"company": zoho, "title": "GST return", "due": (TODAY + timedelta(days=5)).isoformat(),
                               "assignee": "secretary", "source": "Synthetic GST policy", "frequency": "Monthly", "offset": 0})
    api.action(rule, "ruleStatus", {"status": "Under review"})
    api.role("director")
    api.action(rule, "ruleStatus", {"status": "Published"})
    api.role("owner")
    draft = api.create("rule", {"company": abc, "title": "TDS Return", "due": f"{Y}-07-31", "assignee": "accountant",
                                "source": "Synthetic TDS policy", "frequency": "Quarterly", "offset": 0})
    manual = api.create("task", {"company": ps, "title": "Annual filing", "due": f"{Y + 1}-10-05", "assignee": "secretary"})
    rule_before = api.get(rule)
    t = make_template(api)
    plan = {c["id"]: c for c in preview(api, t, [zoho, abc, ps])["companies"]}
    zoho_gst = [e for e in plan[zoho]["entries"] if e["title"] == "GST Return"]
    assert {e["status"] for e in zoho_gst} == {"rule"} and "Rule Library" in zoho_gst[0]["reason"]
    assert any("TDS Return" in w and "not published" in w for w in plan[abc]["warnings"])
    assert {e["status"] for e in plan[abc]["entries"] if e["title"] == "TDS Return"} == {"new"}
    filing = next(e for e in plan[ps]["entries"] if e["title"] == "Annual Filing")
    assert filing["status"] == "duplicate" and filing["existing"]["id"] == manual
    results = {r["company"]: r for r in apply(api, t, [zoho, abc, ps])["results"]}
    assert results[zoho]["created"] == PER_COMPANY - 12 and results[zoho]["ruleCovered"] == 12
    assert results[ps]["created"] == PER_COMPANY - 1
    # Nothing was published, revised or generated from a rule by the bulk setup.
    assert api.get(rule) == rule_before and api.get(draft)["status"] == "Draft"
    assert not any(isinstance(r.get("rule"), dict) for r in api.state()["records"] if r["kind"] == "task")
    # The rule still generates its own obligations, with its own ids, as before.
    planned = api.action(rule, "rulePreview")["plan"]
    assert planned and api.action(rule, "generate")["count"] == len(planned)


# Permissions and isolation -------------------------------------------------------------------

def test_bulk_setup_permissions_are_enforced_by_the_server(desk):
    api, companies = desk
    t = make_template(api)
    api.role("secretary")
    assert "not permitted" in preview(api, t, companies[:1], 403)["error"]
    api.role("owner")
    save_template(api, t, secretaryApply=True)
    api.role("secretary")
    assert preview(api, t, companies[:1])["summary"]["new"] == PER_COMPANY
    assert apply(api, t, companies[:1])["results"][0]["created"] == PER_COMPANY
    body = {"template": t, "templateVersion": 2, "companies": companies[:1], "financialYear": FY}
    for role in ("accountant", "director", "auditor"):
        api.role(role)
        api.v2({"action": "calendarPreview", **body}, 403)
        api.v2({"action": "calendarApply", **body}, 403)
    # A secretary limited to some companies cannot read workspace templates at all.
    api.role("owner")
    scoped = api.v2({"action": "member", "name": "Scoped Secretary", "role": "Secretary", "companies": [companies[1]]})["id"]
    api.role(scoped)
    assert not any(r["kind"] in ("calendartemplate", "calendarbatch") for r in api.state()["records"])
    api.v2({"action": "calendarPreview", **body}, 404)


def test_clients_see_only_their_own_published_entries(api):
    a = api.portal({"action": "companyCreate", "clientName": "Client A", "data": {"name": "Client A Synthetic", "entityType": "Private limited company"}})["id"]
    b = api.portal({"action": "companyCreate", "clientName": "Client B", "data": {"name": "Client B Synthetic", "entityType": "Private limited company"}})["id"]
    client_a = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and a in m["companies"])
    t = make_template(api)
    apply(api, t, [a, b])
    gst = activity_id(api, t, "GST Return")
    api.action(a, "companyStatus", {"inactive": True, "reason": "Synthetic pause"})
    api.role(client_a)
    portal = api.portal()
    mine = [o for o in portal["obligations"] if o.get("calendarTemplate") == t]
    # Only the activity marked client-visible, only for their own company, with the date marked provisional.
    assert len(mine) == 12 and {o["company"] for o in mine} == {a} and {o["title"] for o in mine} == {"GST Return"}
    assert {o["note"] for o in mine} == {"Provisional date: your company secretary will confirm it."}
    assert all("inactive" not in c and "inactiveReason" not in c for c in portal["companies"])
    state = api.state()
    assert {r["kind"] for r in state["records"]} <= {"company", "request", "document"}
    assert all("inactiveReason" not in r for r in state["records"])
    api.v2({"action": "calendarPreview", "template": t, "companies": [a], "financialYear": FY}, 403)
    api.v2({"action": "calendarApply", "template": t, "templateVersion": 1, "companies": [a], "financialYear": FY}, 403)
    api.v2({"action": "templateCreate", "data": {"name": "x", "financialYear": FY, "activities": ACTIVITIES}}, 403)
    # Confirming the date on the internal entry clears the provisional note on the client calendar.
    api.role("owner")
    task = f"tpl-{t}-{a}-{gst}-{Y}-10"
    api.action(task, "confirmDue")
    api.role(client_a)
    item = next(o for o in api.portal()["obligations"] if o.get("sourceTask") == task)
    assert item["note"] == "" and item["due"] == f"{Y}-11-11"


def test_confirming_a_due_date(desk):
    api, companies = desk
    t = make_template(api)
    apply(api, t, companies[:1])
    task = f"tpl-{t}-{companies[0]}-{activity_id(api, t, 'GST Return')}-{Y}-10"
    api.role("director")
    api.action(task, "confirmDue", expect=403)
    api.role("accountant")
    api.action(task, "confirmDue")
    origin = api.get(task)["template"]
    assert origin["dateConfirmed"] is True and origin["confirmedBy"] == "accountant" and origin["confirmedDue"] == f"{Y}-11-11"
    api.action(task, "confirmDue", expect=400)
    api.edit(task, {"due": f"{Y}-11-12"})
    api.action(task, "confirmDue")  # a changed date can be confirmed again
    assert api.get(task)["template"]["confirmedDue"] == f"{Y}-11-12"
    other = api.create("task", {"company": companies[0], "title": "Ad hoc", "due": f"{Y}-06-01", "assignee": "secretary"})
    api.action(other, "confirmDue", expect=400)
    assert any(a.startswith("Confirmed due date") for a in actions(api))


# Reminders and existing calendar functionality ------------------------------------------------

def test_reminders_follow_each_entrys_configuration(desk):
    api, companies = desk
    zoho = companies[0]
    def annual(name, days, overdue):
        return {"name": name, "frequency": "Annual", "dueDay": 1, "monthsAfter": 0, "reminders": {"days": days, "overdue": overdue}}
    t = make_template(api, financialYear=FY_NOW, activities=[annual("Seven days", [7], False), annual("Two days", [7], False),
                                                              annual("Overdue", [], True), annual("Silent", [3], False)])
    ids = {a["name"]: a["id"] for a in api.get(t)["activities"]}
    offsets = {"Seven days": 7, "Two days": 2, "Overdue": -1, "Silent": -2}
    dates = {zoho: {f"{ids[n]}:{FY_NOW}": (TODAY + timedelta(days=d)).isoformat() for n, d in offsets.items()}}
    api.v2({"action": "calendarApply", "template": t, "templateVersion": 1, "companies": [zoho], "financialYear": FY_NOW,
            "dates": dates, "includePast": True})
    plain = api.create("task", {"company": zoho, "title": "Plain task", "due": (TODAY + timedelta(days=2)).isoformat(), "assignee": "secretary"})
    api.v2({"action": "automation"})
    notified = {r["task"] for r in api.state()["records"] if r["kind"] == "notification"}
    entry = lambda name: f"tpl-{t}-{zoho}-{ids[name]}-{FY_NOW}"  # noqa: E731
    assert entry("Seven days") in notified and entry("Overdue") in notified
    assert entry("Two days") not in notified and entry("Silent") not in notified
    assert plain in notified  # work without its own setting keeps the existing three-day rule


def test_existing_individual_calendar_setup_still_works(api):
    c = api.portal({"action": "companyCreate", "clientName": "Individual Client",
                    "data": {"name": "Individual Synthetic", "entityType": "Private limited company"}})["id"]
    t = make_template(api)
    apply(api, t, [c])
    api.portal({"action": "annualPlan", "company": c, "period": FY})
    api.portal({"action": "obligationCreate", "company": c,
                "data": {"title": "Board minutes review", "due": f"{Y}-06-30", "source": "Synthetic policy", "status": "Not started"}})
    task = api.create("task", {"company": c, "title": "Individually scheduled filing", "due": f"{Y}-08-15", "assignee": "secretary"})
    api.portal({"action": "publishTask", "company": c, "task": task})
    tds, gst = activity_id(api, t, "TDS Return"), activity_id(api, t, "GST Return")
    api.portal({"action": "publishTask", "company": c, "task": f"tpl-{t}-{c}-{tds}-{FY}-Q1"})  # not client-visible: publish by hand
    assert "already in the portal" in api.portal({"action": "publishTask", "company": c, "task": f"tpl-{t}-{c}-{gst}-{Y}-10"}, 400)["error"]
    obligations = [o for o in api.portal()["obligations"] if o["company"] == c]
    titles = [o["title"] for o in obligations]
    assert titles.count("GST Return") == 12 and titles.count("TDS Return") == 1
    assert "Individually scheduled filing" in titles and "Board minutes review" in titles
    assert sum(1 for o in obligations if o.get("category") == "Annual compliance") == 6
    ics = api.request("GET", f"/api/portal?export=calendar&company={c}")
    assert ics.status_code == 200 and ics.text.count("BEGIN:VEVENT") == 12 + 1 + 2
