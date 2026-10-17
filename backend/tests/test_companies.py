"""Company master data: one validation rule set and duplicate detection on both creation paths.

Both screens that create or edit a company — the internal Companies module (``/api/v2`` create and
edit) and the Client Portal company profile (``/api/portal`` companyCreate / companySave) — share
``company_profile()`` for validation and ``check_company_duplicates()`` for duplicates, so these
tests drive each rule through both paths over HTTP.
"""
from __future__ import annotations

import pytest

from app.auth import issue_session
from app.services.identity_service import create_organization
from tests.conftest import Api

# A complete, valid Indian company profile; individual tests override one field at a time.
VALID = {
    "name": "Uniqueness Pvt Ltd", "entityType": "Private limited company", "registration": "U72900TN2026PTC123456",
    "pan": "ABCDE1234F", "gstin": "33ABCDE1234F1Z5", "pincode": "600001", "address": "1 Test Road",
}
MALFORMED = (("registration", "not-a-cin"), ("pan", "ABCDE1234"), ("gstin", "33ABCDE1234F1Z"))


def portal_create(api: Api, data: dict, expect: int = 200, **body) -> dict:
    return api.portal({"action": "companyCreate", "data": data, **body}, expect)


def portal_save(api: Api, company: str, data: dict, expect: int = 200) -> dict:
    return api.portal({"action": "companySave", "id": company, "revision": api.get(company)["revision"], "data": data}, expect)


@pytest.fixture
def other_firm(api, client) -> Api:
    """An owner session in a second organization (its own tenant key)."""
    services = client.app.state.services
    with services.db.write() as session:
        _, owner = create_organization(session, "firm-dup", "Firm Dup")
    other = Api(client)
    other.cookie = issue_session(services, owner.id)
    return other


# C1. One validation rule set, whichever screen edits the company ------------------------------

@pytest.mark.parametrize("field,value", MALFORMED)
def test_malformed_identifiers_are_rejected_on_both_paths(api, field, value):
    broken = {**VALID, field: value}
    api.create("company", broken, 400)
    portal_create(api, broken, 400)
    # ... and on both edit paths of an already valid company.
    company = api.create("company", VALID)
    api.edit(company, {field: value}, 400)
    portal_save(api, company, broken, 400)
    assert api.get(company)[field] == VALID[field]


def test_the_two_paths_report_the_same_validation_error(api):
    for field, value in MALFORMED:
        internal = api.request("POST", "/api/v2", json={"action": "create", "kind": "company", "data": {**VALID, field: value}})
        portal = api.request("POST", "/api/portal", json={"action": "companyCreate", "data": {**VALID, field: value}})
        assert internal.status_code == portal.status_code == 400
        assert internal.json()["error"] == portal.json()["error"]


def test_valid_and_optional_identifiers_are_accepted_on_both_paths(api):
    internal = api.create("company", VALID)
    assert api.get(internal)["registration"] == VALID["registration"]
    # A proposed company has no statutory identifiers yet: still valid on either path.
    proposed = api.create("company", {"name": "Proposed Co"})
    portal_proposed = portal_create(api, {"name": "Another Proposed Co"})["id"]
    assert api.get(proposed)["registration"] == "" and api.get(portal_proposed)["pan"] == ""


# C2. Duplicate detection ----------------------------------------------------------------------

def test_an_exact_name_duplicate_warns_but_is_saved(api):
    api.create("company", {"name": "Repeated Name Pvt Ltd"})
    # Normalised for comparison (case and surrounding/repeated whitespace only).
    result = api.v2({"action": "create", "kind": "company", "data": {"name": "  repeated   name   pvt ltd "}})
    assert "already exists" in result["warning"] and result["id"]
    portal = portal_create(api, {"name": "Repeated Name Pvt Ltd"})
    assert "already exists" in portal["warning"]
    assert len([c for c in api.state()["records"] if c["kind"] == "company"]) == 3
    # A different name is saved without a warning.
    assert "warning" not in api.v2({"action": "create", "kind": "company", "data": {"name": "Distinct Co"}})


@pytest.mark.parametrize("field", ("registration", "pan", "gstin"))
def test_duplicate_identifiers_are_rejected_on_both_creation_paths(api, field):
    only = {"name": "First Pvt Ltd", field: VALID[field]}
    api.create("company", only)
    api.create("company", {**only, "name": "Second Pvt Ltd"}, 400)
    portal_create(api, {**only, "name": "Third Pvt Ltd"}, 400)
    # Normalisation: a differently cased / spaced identifier is the same identifier.
    spaced = VALID[field].lower()
    spaced = f" {spaced[:4]} {spaced[4:]} "
    api.create("company", {"name": "Fourth Pvt Ltd", field: spaced}, 400)
    portal_create(api, {"name": "Fifth Pvt Ltd", field: spaced}, 400)
    assert len([c for c in api.state()["records"] if c["kind"] == "company"]) == 1


@pytest.mark.parametrize("field", ("registration", "pan", "gstin"))
def test_duplicate_identifiers_are_rejected_when_editing_on_both_paths(api, field):
    api.create("company", {"name": "Holder Pvt Ltd", field: VALID[field]})
    other = api.create("company", {"name": "Editable Pvt Ltd"})
    api.edit(other, {field: VALID[field]}, 400)
    portal_save(api, other, {"name": "Editable Pvt Ltd", field: VALID[field]}, 400)
    assert api.get(other)[field] == ""
    # A company keeps its own identifiers when it is edited for another reason.
    api.edit(other, {field: VALID[field].lower(), "name": "Editable Pvt Ltd"}, 400)
    holder = next(c for c in api.state()["records"] if c.get("name") == "Holder Pvt Ltd")
    assert api.edit(holder["id"], {"city": "Chennai"})["ok"]
    assert "warning" not in api.edit(holder["id"], {"address": "2 New Road"})


def test_blank_identifiers_never_collide(api):
    first = api.create("company", {"name": "No Identifiers One"})
    second = portal_create(api, {"name": "No Identifiers Two", "registration": "", "pan": "", "gstin": ""})["id"]
    assert first and second


def test_another_organization_may_use_the_same_company(api, other_firm):
    api.create("company", VALID)
    # Uniqueness is per organization: another firm's companies are unrelated data, name included.
    created = other_firm.v2({"action": "create", "kind": "company", "data": VALID})
    assert created["id"] and "warning" not in created
    assert len([c for c in other_firm.state()["records"] if c["kind"] == "company"]) == 1
    # Inside that organization the same identifiers are still rejected, on both paths.
    other_firm.create("company", VALID, 400)
    portal_create(other_firm, VALID, 400)


def test_a_rejected_portal_creation_leaves_no_partial_records(api):
    api.create("company", {"name": "Existing Pvt Ltd", "pan": VALID["pan"]})
    members = len(api.state()["members"])
    portal_create(api, {"name": "Rejected Pvt Ltd", "pan": VALID["pan"]}, 400,
                  clientName="Rejected Client", clientEmail="rejected@firm.test",
                  clientPassword="Portal-duplicate-pass-1", clientConfirmPassword="Portal-duplicate-pass-1")
    state = api.state()
    assert [c["name"] for c in state["records"] if c["kind"] == "company"] == ["Existing Pvt Ltd"]
    assert not any(r["kind"] == "onboarding" for r in state["records"])
    assert len(state["members"]) == members
