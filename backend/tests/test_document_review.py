"""Document review workspace: edited versions, version history and clarification notes.

An edited copy is stored as the document's next version through ``POST /api/v2-files/revision``;
the client's original upload is never changed. ``GET /api/v2-files/versions`` lists the chain as the
caller may see it. ``requestNotes`` saves the reviewer's clarification notes without touching the
request's status, so saving and submitting stay separate actions.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date, timedelta

import pytest

from app.auth import issue_session
from app.core.jsvalues import today as server_today
from app.services.identity_service import create_organization
from tests.conftest import Api

TOMORROW = (date.fromisoformat(server_today()) + timedelta(days=1)).isoformat()
ORIGINAL = b"%PDF-1.4 original client statement"
ANNOTATED = b"%PDF-1.4 annotated by the reviewer"


def xlsx_bytes(marker: str) -> bytes:
    """A small zip with an Office header; the server checks the header, the browser test checks the workbook."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", f"<Types><!-- {marker} --></Types>")
    return buffer.getvalue()


@pytest.fixture
def desk(api):
    company = api.portal({"action": "companyCreate", "clientName": "Priya Client", "data": {"name": "Review Co"}})["id"]
    other = api.portal({"action": "companyCreate", "clientName": "Other Client", "data": {"name": "Elsewhere Co"}})["id"]
    members = api.state()["members"]
    client = next(m["id"] for m in members if m["role"] == "Client" and company in m["companies"])
    request_id = api.create("request", {"company": company, "title": "Bank statements", "due": TOMORROW,
                                        "assignee": client, "notes": "Upload the September statement."})
    return {"api": api, "company": company, "other": other, "client": client, "request": request_id}


def as_role(api: Api, actor: str) -> Api:
    api.role(actor)
    return api


def client_upload(desk, content=ORIGINAL, filename="statement.pdf") -> str:
    api = as_role(desk["api"], desk["client"])
    document = api.upload(desk["company"], content=content, filename=filename, request=desk["request"])
    as_role(api, "owner")
    return document


def save_revision(api: Api, document: str, content: bytes, filename: str = "statement.pdf", expect: int = 200,
                  revision: int | None = None, **fields) -> dict:
    record = next((r for r in api.state()["records"] if r["id"] == document), None)
    data = {"id": document, "revision": str(revision if revision is not None else (record or {}).get("revision", 1)),
            **{k: str(v) for k, v in fields.items()}}
    response = api.request("POST", "/api/v2-files/revision", data=data, files={"file": (filename, content)})
    assert response.status_code == expect, response.text
    return response.json()


def versions(api: Api, document: str, expect: int = 200) -> dict:
    response = api.request("GET", f"/api/v2-files/versions?id={document}")
    assert response.status_code == expect, response.text
    return response.json()


def download(api: Api, document: str) -> bytes:
    response = api.request("GET", f"/api/v2-files?id={document}")
    assert response.status_code == 200, response.text
    return response.content


# Saving edited versions ----------------------------------------------------------------------

def test_an_annotated_pdf_is_saved_as_a_new_version_and_the_original_is_unchanged(desk):
    api = desk["api"]
    original = client_upload(desk)
    history = versions(api, original)
    assert [v["version"] for v in history["versions"]] == [1]
    assert history["versions"][0]["original"] is True and history["editable"] is True
    assert history["request"] == {"id": desk["request"], "title": "Bank statements", "status": "Open"}

    saved = save_revision(api, original, ANNOTATED, note="Highlighted the closing balance")
    assert saved["version"] == 2
    stored = api.get(saved["id"])
    assert stored["previous"] == original and stored["version"] == 2 and stored["request"] == desk["request"]
    assert stored["uploadedBy"] == "owner" and stored["uploadedAt"] and stored["editKind"] == "Annotated"
    assert stored["name"] == "statement.pdf" and stored["company"] == desk["company"]
    assert stored["hash"] == hashlib.sha256(ANNOTATED).hexdigest() and stored["shared"] is False

    # The client's upload keeps its bytes, hash and request link; it is only marked superseded.
    first = api.get(original)
    assert first["supersededBy"] == saved["id"] and first["hash"] == hashlib.sha256(ORIGINAL).hexdigest()
    assert download(api, original) == ORIGINAL and download(api, saved["id"]) == ANNOTATED
    assert api.get(desk["request"])["evidence"] == [original]

    history = versions(api, saved["id"])
    assert [(v["version"], v["original"], v["current"]) for v in history["versions"]] == [(1, True, False), (2, False, True)]
    assert history["versions"][1]["editNote"] == "Highlighted the closing balance"
    assert history["versions"][1]["uploadedBy"] == "Workspace owner"
    assert history["latest"] == saved["id"]
    # Opened from the original, the history is the same chain and points to the latest version.
    assert versions(api, original)["latest"] == saved["id"]
    audit = [a["action"] for a in api.state()["audit"]]
    assert any(a.startswith("Saved edited version 2") for a in audit)


def test_a_second_edit_becomes_version_three(desk):
    api = desk["api"]
    original = client_upload(desk)
    v2 = save_revision(api, original, ANNOTATED)["id"]
    v3 = save_revision(api, v2, ANNOTATED + b" second pass")
    assert v3["version"] == 3
    assert [v["version"] for v in versions(api, original)["versions"]] == [1, 2, 3]
    # Every earlier version is still downloadable.
    assert download(api, original) == ORIGINAL and download(api, v2) == ANNOTATED


def test_spreadsheet_edits_keep_their_format(desk):
    api = desk["api"]
    workbook = client_upload(desk, xlsx_bytes("original"), "ledger.xlsx")
    saved = save_revision(api, workbook, xlsx_bytes("edited"), "ledger.xlsx")
    assert api.get(saved["id"])["editKind"] == "Spreadsheet edit"
    save_revision(api, saved["id"], ANNOTATED, "ledger.pdf", expect=400)  # format change refused
    csv = client_upload(desk, b"account,amount\nCash,10\n", "ledger.csv")
    assert save_revision(api, csv, b"account,amount\nCash,12\n", "ledger.csv")["version"] == 2


def test_invalid_revisions_are_refused(desk):
    api = desk["api"]
    original = client_upload(desk)
    response = api.request("POST", "/api/v2-files/revision", data={"id": original, "revision": "1"},
                           files={"file": ("statement.pdf", b"not a pdf")})
    assert response.status_code == 400 and "PDF header" in response.json()["error"]
    save_revision(api, original, ORIGINAL, expect=400)  # identical bytes: nothing to save
    save_revision(api, original, ANNOTATED, revision=99, expect=409)  # stale revision
    save_revision(api, original, b"%PDF-" + b"x" * (10 * 1024 * 1024), expect=400)  # over 10 MB
    save_revision(api, "no-such-document", ANNOTATED, expect=404)
    text_doc = client_upload(desk, b"plain notes", "notes.txt")
    save_revision(api, text_doc, b"edited notes", "notes.txt", expect=400)  # preview-only format
    v2 = save_revision(api, original, ANNOTATED)["id"]
    # Editing an older version would branch the history.
    error = save_revision(api, original, ANNOTATED + b"!", revision=api.get(original)["revision"], expect=400)
    assert "newer version" in error["error"]
    assert versions(api, original)["latest"] == v2


def test_a_closed_request_makes_its_documents_read_only(desk):
    api = desk["api"]
    original = client_upload(desk)
    as_role(api, desk["client"])
    api.action(desk["request"], "respond", {"response": "Attached."})
    as_role(api, "owner")
    api.action(desk["request"], "requestDecision", {"status": "Closed"})
    history = versions(api, original)
    assert history["editable"] is False and "closed" in history["editBlocked"]
    save_revision(api, original, ANNOTATED, expect=400)


# Who may edit and see what -------------------------------------------------------------------

@pytest.mark.parametrize("role", ["director", "auditor"])
def test_roles_without_edit_rights_can_view_but_not_save(desk, role):
    api = desk["api"]
    original = client_upload(desk)
    as_role(api, role)
    history = versions(api, original)
    assert history["editable"] is False and history["editBlocked"]
    save_revision(api, original, ANNOTATED, expect=403)


@pytest.mark.parametrize("role", ["secretary", "accountant"])
def test_secretaries_and_accountants_can_save_versions(desk, role):
    api = desk["api"]
    original = client_upload(desk)
    as_role(api, role)
    assert save_revision(api, original, ANNOTATED)["version"] == 2


def test_a_client_only_sees_shared_versions(desk):
    api = desk["api"]
    original = client_upload(desk)
    internal = save_revision(api, original, ANNOTATED)["id"]
    published = save_revision(api, internal, ANNOTATED + b" final", shared="true")["id"]

    as_role(api, desk["client"])
    history = versions(api, original)
    assert [v["version"] for v in history["versions"]] == [1, 3]
    assert history["latest"] == published
    assert {v["uploadedBy"] for v in history["versions"]} == {"You", "Your company secretary"}
    versions(api, internal, expect=404)
    response = api.request("GET", f"/api/v2-files?id={internal}")
    assert response.status_code == 404
    assert download(api, published) == ANNOTATED + b" final"


def test_a_client_edits_their_request_document_while_it_awaits_their_answer(desk):
    api = desk["api"]
    original = client_upload(desk)
    as_role(api, desk["client"])
    assert versions(api, original)["editable"] is True
    saved = save_revision(api, original, ANNOTATED, note="Corrected the closing balance")
    as_role(api, "owner")
    stored = api.get(saved["id"])
    # Always visible to the client who made it; the original is untouched.
    assert stored["shared"] is True and stored["uploadedBy"] == desk["client"] and stored["version"] == 2
    assert download(api, original) == ORIGINAL and api.get(desk["request"])["status"] == "Open"

    # Submitted: the documents are fixed while the secretary reviews them.
    as_role(api, desk["client"])
    api.action(desk["request"], "respond", {"response": "Corrected copy attached."})
    history = versions(api, saved["id"])
    assert history["editable"] is False and "review" in history["editBlocked"]
    save_revision(api, saved["id"], ANNOTATED + b" again", expect=400)

    # Returned for correction: editable again.
    as_role(api, "owner")
    api.action(desk["request"], "requestDecision", {"status": "Returned", "reason": "Page 2 is missing."})
    as_role(api, desk["client"])
    assert save_revision(api, saved["id"], ANNOTATED + b" page 2")["version"] == 3


def test_a_client_cannot_edit_documents_outside_their_requests(desk):
    api = desk["api"]
    # A document the staff published to the portal library, not sent for a request.
    library = api.upload(desk["company"], content=ORIGINAL, filename="register.pdf", shared="true")
    # A document on a request addressed to another client of the same company.
    second = api.v2({"action": "member", "name": "Second Client", "role": "Client", "companies": [desk["company"]]})["id"]
    theirs = api.create("request", {"company": desk["company"], "title": "Other request", "due": TOMORROW, "assignee": second})
    as_role(api, second)
    their_doc = api.upload(desk["company"], content=ORIGINAL, filename="theirs.pdf", request=theirs)

    as_role(api, desk["client"])
    history = versions(api, library)
    assert history["editable"] is False and "your requests" in history["editBlocked"]
    save_revision(api, library, ANNOTATED, expect=400)
    save_revision(api, their_doc, ANNOTATED, expect=400)  # visible (shared) but not their request


def test_staff_scoped_to_another_company_cannot_reach_the_document(desk):
    api = desk["api"]
    original = client_upload(desk)
    outsider = api.v2({"action": "member", "name": "Elsewhere Secretary", "role": "Secretary", "companies": [desk["other"]]})["id"]
    as_role(api, outsider)
    versions(api, original, expect=404)
    save_revision(api, original, ANNOTATED, expect=404)


def test_another_organization_cannot_reach_the_document(desk, client):
    original = client_upload(desk)
    services = client.app.state.services
    with services.db.write() as session:
        _org, owner_b = create_organization(session, "firm-document-review", "Firm B")
    firm_b = Api(client)
    firm_b.cookie = issue_session(services, owner_b.id)
    versions(firm_b, original, expect=404)
    response = firm_b.request("POST", "/api/v2-files/revision", data={"id": original, "revision": "1"},
                              files={"file": ("statement.pdf", ANNOTATED)})
    assert response.status_code == 404


# Clarification notes and the separate submit step --------------------------------------------

NOTES = "**Observations**\n- Closing balance does not match the ledger\n1. Re-export the statement"


def test_clarification_notes_are_saved_without_changing_the_request(desk):
    api = desk["api"]
    before = api.get(desk["request"])
    api.action(desk["request"], "requestNotes", {"clarification": NOTES})
    after = api.get(desk["request"])
    assert after["clarification"] == NOTES and after["clarificationBy"] == "owner" and after["clarificationAt"]
    assert after["status"] == before["status"] == "Open" and after["evidence"] == before["evidence"]

    # Editing the request elsewhere keeps the notes.
    api.edit(desk["request"], {"title": "September bank statements"})
    assert api.get(desk["request"])["clarification"] == NOTES

    as_role(api, desk["client"])
    item = next(i for i in api.portal()["work"] if i["id"] == desk["request"])
    assert item["request"]["clarification"] == NOTES and item["request"]["clarificationAt"]
    seen = next(r for r in api.state()["records"] if r["id"] == desk["request"])
    assert seen["clarification"] == NOTES and "clarificationBy" not in seen
    api.action(desk["request"], "requestNotes", {"clarification": "client edit"}, expect=403)


def test_saving_a_version_does_not_submit_the_request(desk):
    api = desk["api"]
    original = client_upload(desk)
    save_revision(api, original, ANNOTATED)
    request = api.get(desk["request"])
    assert request["status"] == "Open" and "respondedAt" not in request
    # Submitting is its own action, as before.
    as_role(api, desk["client"])
    api.action(desk["request"], "respond", {"response": "Statement attached."})
    as_role(api, "owner")
    assert api.get(desk["request"])["status"] == "Responded"
    # Notes can still be saved while the response is under review, not once closed.
    api.action(desk["request"], "requestNotes", {"clarification": NOTES})
    api.action(desk["request"], "requestDecision", {"status": "Closed"})
    api.action(desk["request"], "requestNotes", {"clarification": "late"}, expect=400)


# Static chunks for the lazily loaded workspace -----------------------------------------------

def test_code_split_chunks_are_served_by_strict_name_only(api, settings):
    chunks = settings.governance_frontend_dir / "chunks"
    chunks.mkdir()
    (chunks / "viewer-ABC123.js").write_text("export default 1")
    (chunks / "exceljs.min-XYZ9.js").write_text("export default 2")
    (settings.governance_frontend_dir / "secret.js").write_text("nope")
    ok = api.request("GET", "/chunks/viewer-ABC123.js")
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/javascript")
    assert api.request("GET", "/chunks/exceljs.min-XYZ9.js").text == "export default 2"
    for bad in ("/chunks/..%2Fsecret.js", "/chunks/missing.js", "/chunks/viewer.txt", "/chunks/.hidden.js", "/chunks/a..js", "/chunks/%2E%2E%2Fsecret.js"):
        assert api.request("GET", bad).status_code == 404, bad
    assert api.request("GET", "/pdf.worker.js").status_code == 503  # not built in this test folder
