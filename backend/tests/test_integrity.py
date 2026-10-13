"""Database-level business rules (migration 0004) and safe text storage, on SQLite and PostgreSQL.

These guarantees back up the application checks when requests race; here they are exercised
directly at the repository level (as code that skipped the application check would hit them).
Run on PostgreSQL with GOVERNANCE_TEST_DATABASE=postgres.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.api.deps import services_of
from app.core.errors import Conflict
from app.repositories.auth import AuthRepository
from app.repositories.records import RecordRepository
from app.services.identity_service import identity_ids

WS = "local-preview"
TODAY = date.today().isoformat()


def _issued_invoice(api, company: str, number: str) -> str:
    data = {"number": number, "due": TODAY, "items": [{"description": "Fee", "quantity": 1, "rate": "100"}],
            "seller": {"name": "Firm", "address": "Street"}}
    invoice = api.portal({"action": "invoiceCreate", "company": company, "data": data})["id"]
    api.portal({"action": "invoiceIssue", "company": company, "id": invoice, "revision": api.get(invoice)["revision"]})
    return invoice


@pytest.fixture
def billing(api):
    company = api.create("company", {"name": "Billing Co"})
    api.portal({"action": "companySave", "id": company, "revision": api.get(company)["revision"], "data": {"name": "Billing Co", "address": "1 Road"}})
    return company


def test_issued_invoice_numbers_are_unique_in_the_database(api, client, billing):
    _issued_invoice(api, billing, "INV-1")
    draft = api.portal({"action": "invoiceCreate", "company": billing, "data": {"number": "INV-1", "due": TODAY, "items": [
        {"description": "Fee", "quantity": 1, "rate": "100"}], "seller": {"name": "Firm", "address": "Street"}}})["id"]  # drafts may share it
    record = api.get(draft)
    with pytest.raises(Conflict, match="Invoice number already issued"):
        with services_of(client).db.write() as session:
            RecordRepository(session, WS).update_if_revision(draft, record["revision"], {**record, "status": "Issued"})
    assert api.get(draft)["status"] == "Draft"


def test_payment_references_are_unique_per_company_in_the_database(api, client, billing):
    invoice = _issued_invoice(api, billing, "INV-2")
    proof = api.upload(billing, shared="true")
    api.portal({"action": "paymentReport", "company": billing, "id": invoice, "revision": api.get(invoice)["revision"], "amount": "10",
                "reference": "UTR-77", "paidAt": TODAY, "evidence": [proof]})
    duplicate = {"company": billing, "invoice": invoice, "amount": 500, "reference": "utr-77", "status": "Pending verification"}
    with pytest.raises(Conflict, match="already been reported"):
        with services_of(client).db.write() as session:
            RecordRepository(session, WS).insert("payment-copy", "payment", duplicate)
    # A rejected report frees its reference, as the application rule says.
    with services_of(client).db.write() as session:
        RecordRepository(session, WS).insert("payment-rejected", "payment", {**duplicate, "status": "Rejected"})


def test_a_trial_balance_file_is_imported_once_in_the_database(api, client):
    company = api.create("company", {"name": "Ledger Co"})
    first = api.trial_balance(company)
    original = api.get(first)
    copy = {k: v for k, v in original.items() if k not in ("id", "kind", "revision", "updated")}
    with pytest.raises(Conflict, match="already been imported"):
        with services_of(client).db.write() as session:
            RecordRepository(session, WS).insert("tb-copy", "trialbalance", copy)
    with services_of(client).db.write() as session:  # another period is a different import
        RecordRepository(session, WS).insert("tb-other-period", "trialbalance", {**copy, "period": "2026-08"})


def test_an_email_belongs_to_one_account(api, client):
    services = services_of(client)
    secretary, _ = identity_ids(WS, "secretary")
    accountant, _ = identity_ids(WS, "accountant")
    with services.db.write() as session:
        AuthRepository(session).set_email(secretary, "person@firm.test")
    with pytest.raises(Conflict, match="already belongs to another account"):
        with services.db.write() as session:
            AuthRepository(session).set_email(accountant, "PERSON@firm.test")


def test_nul_characters_are_dropped_not_fatal(api):
    company = api.create("company", {"name": "Nul\u0000 Co"})
    task = api.create("task", {"company": company, "title": "Ti\u0000tle", "due": TODAY})
    assert api.get(company)["name"] == "Nul Co" and api.get(task)["title"] == "Title"


def test_nul_characters_in_any_input_never_reach_the_database(api):
    company = api.create("company", {"name": "Clean Co"})
    member = api.v2({"action": "member", "name": "Na\u0000me", "role": "Secretary", "companies": [company]})["id"]
    assert next(m for m in api.state()["members"] if m["id"] == member)["name"] == "Name"
    assert any(a["subject"] == "Name" for a in api.state()["audit"])
    # A hand-built multipart body with raw NUL bytes in a field and in the file name.
    boundary = "gd-boundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"company\"\r\n\r\n{company}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"period\"\r\n\r\n2026\x00-09\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"re\x00port.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\nevidence\r\n--{boundary}--\r\n").encode()
    response = api.request("POST", "/api/v2-files", content=body, headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert response.status_code == 200, response.text
    stored = api.get(response.json()["id"])
    assert stored["name"] == "report.txt" and stored["period"] == "2026-09"
    doc = stored["id"]
    # Query parameters: %00 is removed before any route sees it.
    assert api.request("GET", "/api/v2/records?company=%00x").status_code == 200
    assert api.request("GET", f"/api/v2-files?id={doc}%00").status_code == 200
    export = api.request("GET", "/api/v2?export=pack&id=%00")  # empty id: the CSV report, as before
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
