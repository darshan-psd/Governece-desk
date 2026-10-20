"""Unit tests for the ported rules (from tests/workflow-rules.test.mjs and tests/domain.test.mjs)."""
import pytest

from app.core.errors import BadRequest
from app.core.jsvalues import date_valid, js_number, js_string, text, truthy
from app.domain.accounting import parse_trial_balance
from app.domain.portal_domain import check_company_duplicates, company_profile, invoice_data, money
from app.domain.workflow_rules import can_access, clean_checklist, csv_cell, escape_html, next_date, review_problems, validate_record
from app.services.portal.exports import money_text


def test_calendar_dates_and_recurrence():
    assert date_valid("2026-02-29") is False
    assert date_valid("2028-02-29") is True
    assert next_date("2026-01-31", "Monthly") == "2026-02-28"
    assert next_date("2026-01-31", "Monthly", 2) == "2026-03-31"
    assert next_date("2028-02-29", "Annual") == "2029-02-28"
    assert next_date("2026-11-30", "Quarterly") == "2027-02-28"


def test_rule_validation_and_access():
    with pytest.raises(BadRequest):
        validate_record("rule", {"title": "Rule", "company": "c", "due": "2026-10-01", "frequency": "Monthly", "source": "Policy", "offset": -1})
    with pytest.raises(BadRequest):
        validate_record("task", {"title": "Test", "company": "c", "due": "2026-02-30"})
    with pytest.raises(BadRequest):
        validate_record("company", {"name": " "})
    assert can_access({"role": "Client", "companies": ["a"]}, {"kind": "company", "id": "b"}) is False
    assert can_access({"role": "Client", "companies": ["a"]}, {"kind": "task", "company": "a"}) is True


def test_checklist_steps_need_a_label():
    """A step with no text is a half-finished edit: it used to be dropped silently, which lost a
    preparation checklist behind a "Saved successfully" message."""
    kept = clean_checklist([{"id": "a", "label": "Reconcile bank", "done": True},
                            {"id": "b", "label": " Review journals ", "done": False}])
    assert kept == [{"id": "a", "label": "Reconcile bank", "done": True},
                    {"id": "b", "label": "Review journals", "done": False}]  # ids and state kept
    assert clean_checklist([]) == [] and clean_checklist(None) == []
    for broken in ([{"label": ""}], [{"label": "   "}], [{"label": "\t\n"}], [{"done": True}], [{}]):
        with pytest.raises(BadRequest, match="Give every checklist step a label."):
            clean_checklist(broken)
    with pytest.raises(BadRequest, match="Give every checklist step a label."):  # one bad step in a valid list
        clean_checklist([{"label": "Reconcile bank", "done": True}, {"label": "", "done": True},
                         {"label": "Review journals"}])
    # A step added without a label is refused for both kinds that carry a checklist.
    for kind in ("task", "close"):
        base = {"title": "Preparation", "company": "c", "due": "2026-10-01",
                "checklist": [{"label": "Reconcile bank", "done": True}, {"label": "", "done": True}]}
        with pytest.raises(BadRequest, match="Give every checklist step a label."):
            validate_record(kind, base)
        assert len(validate_record(kind, {**base, "checklist": base["checklist"][:1]})["checklist"]) == 1
    # Steps without an id are still given one.
    assert clean_checklist([{"label": "Reconcile bank"}])[0]["id"]


def test_meeting_quorum_validation():
    base = {"title": "Board", "company": "c", "due": "2026-10-01"}
    assert validate_record("meeting", {**base, "quorum": "3"})["quorum"] == 3
    assert validate_record("meeting", base)["quorum"] == 1
    for bad in (0.5, "2.5", 101, "x"):
        with pytest.raises(BadRequest):
            validate_record("meeting", {**base, "quorum": bad})


def test_review_problems():
    docs = [{"id": "d", "kind": "document", "company": "a"}]
    problems = review_problems({"company": "a", "evidence": ["d", "unknown"], "preparer": "one"}, docs, {"id": "two"})
    assert "All evidence must belong to this company." in problems
    assert "A different team member must approve the work." in review_problems({"company": "a", "evidence": ["d"], "preparer": "one"}, docs, {"id": "one"})
    meeting = {"kind": "meeting", "company": "a", "evidence": ["d"], "preparer": "one", "quorum": 2, "agenda": "A", "minutes": "M",
               "attendees": [{"name": "A", "present": True}, {"name": "B", "present": True, "conflict": True}]}
    assert any("quorum" in p for p in review_problems(meeting, docs, {"id": "two"}))
    meeting["attendees"][1]["conflict"] = False
    assert review_problems(meeting, docs, {"id": "two"}) == []


def test_exports_are_escaped():
    assert csv_cell("=SUM(A1)").startswith("\"'")
    assert csv_cell('say "hi"') == '"say ""hi"""'
    assert escape_html("<script>'&\"") == "&lt;script&gt;&#39;&amp;&quot;"


def test_trial_balance_parser():
    assert parse_trial_balance("account,debit,credit\nCash,100.10,0\nCapital,0,100.10")["balanced"] is True
    assert parse_trial_balance('account,debit,credit\n"Cash, bank",100,0')["difference"] == 10000
    for bad in ("account,debit,credit\nCash,-2,0", "account,debit,credit\nCash,1.001,0", "name,amount\nCash,3", "account,debit,credit\nCash,١٠,0"):
        with pytest.raises(BadRequest):
            parse_trial_balance(bad)


def test_inr_money_and_invoices():
    assert money("1250.25") == 125025
    assert money(100) == 10000
    for bad in ("-1", "1.001", "12abc", "1e3"):
        with pytest.raises(BadRequest):
            money(bad)
    invoice = invoice_data({"due": "2026-10-01", "items": [{"description": "Service", "quantity": 2, "rate": "1250.25"}], "tax": "0",
                            "seller": {"name": "Firm", "address": "Street"}})
    assert invoice["total"] == 250050
    assert money_text(15005000) == "INR 1,50,050.00"
    assert money_text(99) == "INR 0.99"


def test_company_profile_india_formats():
    profile = {"name": "Co", "registration": "U72900TN2026PTC123456", "pan": "ABCDE1234F", "pincode": "600001",
               "shareholders": [{"name": "A", "holding": 100}]}
    assert company_profile(profile)["registration"] == "U72900TN2026PTC123456"
    for field, value in (("registration", "bad"), ("pincode", "60001"), ("pan", "ABCDE1234"), ("gstin", "123")):
        with pytest.raises(BadRequest):
            company_profile({**profile, field: value})
    with pytest.raises(BadRequest):
        company_profile({**profile, "shareholders": [{"name": "A", "holding": 101}]})
    with pytest.raises(BadRequest):
        company_profile({**profile, "authorisedCapital": "10", "paidCapital": "20"})


def test_internal_company_edits_use_the_same_validation_as_the_portal():
    """Decision: the internal Companies create/edit path and the Client Portal path validate
    through the same function, so a malformed statutory identifier is rejected identically
    regardless of which screen it was entered from."""
    with pytest.raises(BadRequest):
        validate_record("company", {"name": "Co", "registration": "not-a-cin"})
    with pytest.raises(BadRequest):
        validate_record("company", {"name": "Co", "pan": "bad-pan"})
    # A field the portal schema doesn't have (framework) is still accepted and preserved, not
    # silently dropped on the next edit of an existing record.
    out = validate_record("company", {"name": "Co", "framework": "Ind AS"})
    assert out["framework"] == "Ind AS" and out["jurisdiction"] == "India"


def test_check_company_duplicates():
    existing = [
        {"id": "c1", "name": "Acme Pvt Ltd", "registration": "U72900TN2026PTC123456", "pan": "ABCDE1234F", "gstin": ""},
        {"id": "c2", "name": "Other Co", "registration": "", "pan": "", "gstin": ""},
    ]
    # A CIN/PAN/GSTIN match against another company is a hard block (normalised: case/whitespace).
    with pytest.raises(BadRequest):
        check_company_duplicates(existing, {"name": "New Co", "registration": " u72900tn2026ptc123456 "})
    with pytest.raises(BadRequest):
        check_company_duplicates(existing, {"name": "New Co", "pan": "abcde1234f"})
    # Blank identifiers never collide with each other.
    assert check_company_duplicates(existing, {"name": "New Co", "registration": ""}) is None
    # A name match warns but does not block — two real companies can share a common name.
    warning = check_company_duplicates(existing, {"name": " acme pvt ltd "})
    assert warning and "already exists" in warning
    # Editing a company against itself (exclude_id) is never a duplicate of its own data.
    assert check_company_duplicates(existing, {"name": "Acme Pvt Ltd", "registration": "U72900TN2026PTC123456"}, exclude_id="c1") is None
    # A genuinely different company with an unrelated name/identifier is unaffected.
    assert check_company_duplicates(existing, {"name": "Brand New Co", "registration": "U11111TN2026PTC999999"}) is None


def test_javascript_value_semantics():
    assert truthy([]) and truthy({}) and truthy("0")
    assert not truthy("") and not truthy(0) and not truthy(None)
    assert js_string(100.0) == "100" and js_string(None) == "null" and js_string(True) == "true"
    assert js_number("3") == 3 and js_number("") == 0 and js_number(True) == 1
    assert text("  hello  ") == "hello" and text(None) == "" and text("x" * 600) == "x" * 500
