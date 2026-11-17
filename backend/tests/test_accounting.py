"""Accounting: trial-balance CSV imports, their versions and review, and the financial close.

Covers the gaps found by the accounting audit of 2026-11-11: concurrent imports, cancelled work
flagged for review, non-UTF-8 files, file-name case, blank-looking amounts, all-zero files,
repeated reviews and the close's category.
"""
from __future__ import annotations

import threading
from datetime import date

import pytest

from app.api.deps import services_of
from app.auth import Principal
from app.repositories.identity import IdentityRepository
from app.repositories.records import RecordRepository
from app.services import documents_service
from app.services.context import load_context
from app.services.documents_service import IncomingFile

WS = "local-preview"
TODAY = date.today().isoformat()
CSV = "account,debit,credit\nCash,100,0\nCapital,0,100"


@pytest.fixture
def ledger(api):
    company = api.create("company", {"name": "Ledger One"})
    common = {"company": company, "title": "September close", "due": TODAY, "assignee": "owner"}
    return api, company, common


def imported(api, company, content: str | bytes = CSV, filename: str = "tb.csv", period: str = "2026-09",
             currency: str = "INR", expect: int = 200) -> str:
    """The new import's id, or the error message when ``expect`` is not 200."""
    body = content.encode() if isinstance(content, str) else content
    response = api.request("POST", "/api/v2-import", data={"company": company, "period": period, "currency": currency},
                           files={"file": (filename, body)})
    assert response.status_code == expect, response.text
    return response.json()["id" if expect == 200 else "error"]


def reviewed(api, company, **kwargs) -> str:
    tb = imported(api, company, **kwargs)
    api.role("accountant")
    api.action(tb, "reviewImport")
    api.role("owner")
    return tb


def approved_close(api, company, common, tb) -> str:
    close = api.create("close", {**common, "period": "2026-09", "trialbalance": tb, "evidence": [api.upload(company)]})
    api.move(close, "In progress")
    api.move(close, "Under review")
    api.role("secretary")
    api.move(close, "Approved")
    api.role("owner")
    return close


# CSV files ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "account,debit,credit\r\nCash,100.50,0\r\nCapital,0,100.50\r\n",  # Windows line endings
    "﻿Account,Debit,Credit\nCash,1,0\nCapital,0,1",  # Excel "CSV UTF-8" byte-order mark
    "code,credit,account,debit\n1,0,Cash,5\n2,5,Capital,0",  # columns in any order, extra columns ignored
    "account,debit,credit\nCash,5,\nCapital,,5",  # empty cells are zero
    "account,debit,credit\nCash,5, \nCapital, ,5",  # so are cells holding only spaces
    'account,debit,credit\n"Sundry, debtors",5,0\nCapital,0,5',  # quoted account names
], ids=["crlf", "bom", "reordered", "blank-cells", "space-cells", "quoted"])
def test_valid_trial_balance_files_import(ledger, content):
    api, company, _ = ledger
    assert imported(api, company, content)


@pytest.mark.parametrize("content,message", [
    ("account,debit,credit\nCash,101,0\nCapital,0,100", "differ by 1.00 INR"),
    ("account,debit,credit\nCash,-100,0\nCapital,0,-100", "Invalid amount on row 2"),
    ("account,debit,credit\nCash,1.005,0\nCapital,0,1.005", "Invalid amount on row 2"),
    ('account,debit,credit\nCash,"1,000",0\nCapital,0,"1,000"', "Invalid amount on row 2"),
    ("account,debit,credit\n,5,0\nCapital,0,5", "Account is missing on row 2"),
    ("name,dr,cr\nCash,5,0\nCapital,0,5", "account, debit and credit columns"),
    ("account,debit,credit\n", "between 1 and 2,000"),
    ('account,debit,credit\n"Cash,5,0\nCapital,0,5', "unfinished quoted field"),
    ("account,debit,credit\nCash,0,0\nCapital,0,0", "Every amount in the file is zero"),
], ids=["unbalanced", "negative", "three-decimals", "thousand-separator", "no-account", "header", "empty", "quote", "all-zero"])
def test_invalid_trial_balance_files_are_refused(ledger, content, message):
    api, company, _ = ledger
    assert message in imported(api, company, content, expect=400)


def test_file_name_case_encoding_and_size(ledger):
    api, company, _ = ledger
    assert imported(api, company, filename="TRIAL_BALANCE.CSV")  # Windows often saves upper-case names
    assert imported(api, company, CSV, filename="tb.xlsx", expect=400) == "Choose a .csv file."
    big = "account,debit,credit\n" + "Cash,1,0\nCapital,0,1\n" * 60000
    assert imported(api, company, big, expect=400) == "The CSV file is larger than 1 MB."
    # A file saved in a legacy Windows encoding is refused instead of being stored with garbled names.
    legacy = "account,debit,credit\nCafé £ account,5,0\nCapital,0,5".encode("cp1252")
    assert "UTF-8" in imported(api, company, legacy, expect=400)
    utf8 = imported(api, company, "account,debit,credit\nCafé ₹ account,5,0\nCapital,0,5")
    assert api.get(utf8)["entries"][0]["account"] == "Café ₹ account"


# Versions and review -----------------------------------------------------------------------------

def test_versions_follow_company_period_and_currency(ledger):
    api, company, _ = ledger
    other = api.create("company", {"name": "Ledger Two"})
    first = imported(api, company)
    assert "already been imported" in imported(api, company, expect=400)
    assert imported(api, other) and imported(api, company, period="2026-10")
    assert api.get(imported(api, company, currency="USD"))["version"] == 1
    second = imported(api, company, "account,debit,credit\nCash,200,0\nCapital,0,200")
    assert api.get(second)["version"] == 2 and api.get(second)["previousImport"] == first


def test_concurrent_imports_get_consecutive_versions(ledger, client):
    """Two different files for one company and period imported at the same moment used to both
    become "version 2" (both reviewable, both counted as latest)."""
    api, company, _ = ledger
    first = imported(api, company)
    services = services_of(client)
    with services.db.read() as session:
        membership = IdentityRepository(session).membership_by_member(WS, "owner")
    barrier = threading.Barrier(2, timeout=10)
    store = services.storage.put
    results: list = []

    def held_put(key, data):  # both imports have passed every check made before storing the file
        barrier.wait()
        store(key, data)

    def run(amount):
        ctx = load_context(services, Principal(membership.id))
        file = IncomingFile("tb.csv", f"account,debit,credit\nCash,{amount},0\nCapital,0,{amount}".encode())
        try:
            results.append(documents_service.import_trial_balance(services, ctx, {"company": company, "period": "2026-09", "currency": "INR"}, file))
        except Exception as error:  # noqa: BLE001 - reported by the assertion below
            results.append(error)

    services.storage.put = held_put
    try:
        threads = [threading.Thread(target=run, args=(amount,)) for amount in (300, 400)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        services.storage.put = store
    assert all(isinstance(r, dict) and r["ok"] for r in results), results
    chain = sorted((r for r in api.state()["records"] if r["kind"] == "trialbalance" and r["company"] == company), key=lambda r: r["version"])
    assert [r["version"] for r in chain] == [1, 2, 3]
    assert [r["previousImport"] for r in chain] == [None, first, chain[1]["id"]]


def test_an_import_is_reviewed_once_by_someone_else(ledger):
    api, company, _ = ledger
    tb = imported(api, company)
    assert "different reviewer" in api.action(tb, "reviewImport", expect=400)["error"]
    api.role("accountant")
    api.action(tb, "reviewImport")
    api.role("secretary")
    assert api.action(tb, "reviewImport", expect=400)["error"] == "This import has already been reviewed."
    assert api.get(tb)["reviewedBy"] == "accountant"


def test_import_and_review_roles(ledger):
    api, company, _ = ledger
    for role in ("director", "auditor"):
        api.role(role)
        imported(api, company, expect=403)
    api.role("accountant")
    tb = imported(api, company)
    api.role("auditor")
    assert api.get(tb)  # read only
    api.action(tb, "reviewImport", expect=403)


# Financial close ---------------------------------------------------------------------------------

def test_close_lifecycle_category_and_approval_roles(ledger):
    api, company, common = ledger
    tb = reviewed(api, company)
    close = api.create("close", {**common, "period": "2026-09", "trialbalance": tb, "evidence": [api.upload(company)],
                                 "category": "Compliance"})
    assert api.get(close)["category"] == "Financial close"
    api.move(close, "In progress")
    api.move(close, "Under review")
    api.role("accountant")
    api.move(close, "Approved", 403)  # accountants prepare; they do not approve
    api.role("director")
    api.move(close, "Approved")
    assert api.get(close)["approval"]["trialbalance"] == tb
    api.move(close, "Completed", 403)  # directors approve; preparers complete
    api.role("owner")
    api.move(close, "Completed")


def test_close_must_match_the_trial_balance(ledger):
    api, company, common = ledger
    tb = reviewed(api, company)
    other = api.create("company", {"name": "Ledger Two"})
    foreign = reviewed(api, other)
    api.create("close", {**common, "period": "2026-10", "trialbalance": tb}, 400)
    api.create("close", {**common, "period": "2026-09", "trialbalance": foreign}, 400)
    api.create("close", {**common, "period": "2026-13"}, 400)
    close = api.create("close", {**common, "period": "2026-09", "trialbalance": tb})
    api.edit(close, {"period": "2026-10"}, 400)


def test_a_new_import_flags_linked_closes_but_not_cancelled_ones(ledger):
    api, company, common = ledger
    tb = reviewed(api, company)
    completed = approved_close(api, company, common, tb)
    api.role("secretary")
    api.move(completed, "Completed")
    api.role("owner")
    cancelled = api.create("close", {**common, "title": "Raised in error", "period": "2026-09", "trialbalance": tb})
    api.action(cancelled, "transition", {"status": "Cancelled", "reason": "Duplicate close"})
    revision = api.get(cancelled)["revision"]
    imported(api, company, "account,debit,credit\nCash,900,0\nCapital,0,900")
    assert api.get(completed)["reviewRequired"] is True
    assert not api.get(cancelled).get("reviewRequired") and api.get(cancelled)["revision"] == revision


def test_a_new_document_version_does_not_flag_cancelled_work(ledger):
    api, company, common = ledger
    evidence = api.upload(company)
    task = api.create("task", {**common, "title": "Cancelled filing", "evidence": [evidence]})
    api.action(task, "transition", {"status": "Cancelled", "reason": "No longer required"})
    live = api.create("task", {**common, "title": "Live filing", "evidence": [evidence]})
    api.upload(company, previous=evidence, content=b"Evidence v2")
    assert api.get(live)["reviewRequired"] is True
    assert not api.get(task).get("reviewRequired")


def test_the_client_portal_keeps_a_cancelled_close_cancelled(api, client):
    company = api.portal({"action": "companyCreate", "clientName": "Ledger client", "data": {"name": "Ledger Portal Co"}})["id"]
    client_id = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and company in m["companies"])
    tb = imported(api, company, period="2026-01")
    close = api.create("close", {"company": company, "title": "January close", "due": TODAY, "assignee": "owner",
                                 "period": "2026-01", "trialbalance": tb})
    api.portal({"action": "publishTask", "company": company, "task": close})
    api.action(close, "transition", {"status": "Cancelled", "reason": "Raised in error"})

    def portal_status() -> str:
        api.role(client_id)
        status = next(i for i in api.portal()["work"] if i["title"] == "January close")["status"]
        api.role("owner")
        return status

    assert portal_status() == "Cancelled"
    imported(api, company, "account,debit,credit\nCash,700,0\nCapital,0,700", period="2026-01")
    assert portal_status() == "Cancelled"
    # A cancelled close already flagged by an earlier version of the app still shows as cancelled.
    record = api.get(close)
    with services_of(client).db.write() as session:
        data = {k: v for k, v in record.items() if k not in ("id", "kind", "revision", "updated")}
        assert RecordRepository(session, WS).update_if_revision(close, record["revision"], {**data, "reviewRequired": True})
    assert portal_status() == "Cancelled"
