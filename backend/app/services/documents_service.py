"""Documents and trial-balance imports.

Ported from ``app/api/v2-files/route.ts``, ``app/api/v2-import/route.ts`` and the documentShare /
reviewImport actions of ``lib/phase-service.ts``.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from app.core.container import Services
from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.jsvalues import as_list, now_iso, text, truthy
from app.domain.accounting import parse_trial_balance
from app.domain.portal_domain import DOCUMENT_CATEGORIES
from app.domain.workflow_rules import EDIT_ROLES
from app.permissions import Context, accessible, authorize
from app.repositories.records import AuditRepository, RecordRepository
from app.services.context import audit_label, save
from app.storage import tenant_key

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = ("pdf", "txt", "csv", "xlsx", "docx")
CURRENCIES = ("INR", "GBP", "USD", "SGD", "EUR", "AUD")
REVIEW_ON_NEW_VERSION = ("task", "meeting", "close", "event", "submission")
# An information request takes documents while the client still owes an answer: the same statuses
# in which ``governance_service.respond`` accepts one.
ANSWERABLE_REQUEST = ("Open", "Returned")


@dataclass
class IncomingFile:
    name: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


def _field(form: dict, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _company_accessible(ctx: Context, company_id: str) -> bool:
    company = ctx.find(company_id, "company")
    return company is not None and accessible(ctx, company)


def _answerable_request(ctx: Context, request_id: str, company: str) -> dict:
    """The information request an upload answers.

    It must be visible to the uploader — a client only sees the requests addressed to them — so a
    forged id, another client's request or another organization's is reported exactly like a
    missing one. It must belong to the upload's company and still be waiting for an answer.
    """
    request = ctx.find(request_id, "request")
    if request is None or not accessible(ctx, request):
        raise NotFound("Request not found.")
    if request.get("company") != company:
        raise BadRequest("Upload the document to the request's own company.")
    if request.get("status") not in ANSWERABLE_REQUEST:
        raise BadRequest("This request is not accepting documents.")
    return request


def _extension(name: str) -> str:
    return name.split(".")[-1].lower()


def _validate_file(file: IncomingFile | None) -> str:
    """Size, extension and content-header checks shared by every document upload; returns the extension."""
    if file is None or file.size == 0 or file.size > MAX_DOCUMENT_BYTES:
        raise BadRequest("Choose a file between 1 byte and 10 MB.")
    extension = _extension(file.name)
    if extension not in ALLOWED_EXTENSIONS:
        raise BadRequest("Supported formats: PDF, TXT, CSV, XLSX and DOCX.")
    start = file.content[:5]
    if extension == "pdf" and start.decode("utf-8", errors="replace") != "%PDF-":
        raise BadRequest("This file does not have a valid PDF header.")
    if extension in ("xlsx", "docx") and start[:2] != b"PK":
        raise BadRequest("This file does not have a valid Office document header.")
    return extension


def upload_document(services: Services, ctx: Context, form: dict, file: IncomingFile | None) -> dict:
    authorize(ctx, ["Owner", "Secretary", "Accountant", "Client", "Director"])
    company = _field(form, "company")
    if not _company_accessible(ctx, company):
        raise Forbidden("Company access is required.")
    request = _answerable_request(ctx, _field(form, "request"), company) if _field(form, "request") else None
    _validate_file(file)

    previous = _field(form, "previous")
    parent = None
    if previous:
        parent = ctx.find(previous, "document")
        if parent is not None and (parent.get("company") != company or not accessible(ctx, parent)):
            parent = None
        if parent is None:
            raise BadRequest("Choose a previous version from this company.")
    # ``previous`` is only set on documents, and every version shares the parent's company.
    if (parent and truthy(parent.get("supersededBy"))) or (previous and ctx.exists(kinds=["document"], where={"company": company, "previous": previous})):
        raise BadRequest("A newer version already exists. Upload against the latest version.")
    if ctx.role == "Client" and previous:
        raise Forbidden("Clients can upload new evidence but cannot replace controlled documents.")

    document_id = str(uuid.uuid4())
    key = f"{ctx.owner}/{document_id}"
    digest = hashlib.sha256(file.content).hexdigest()
    services.storage.put(key, file.content)
    category = _field(form, "category") or "Other"
    if category not in DOCUMENT_CATEGORIES:
        services.storage.delete(key)
        raise BadRequest("Select a valid document category.")
    data = {
        "category": category, "period": _field(form, "period")[:30], "name": file.name[:255], "company": company,
        "key": key, "size": file.size, "hash": digest, "version": ((parent or {}).get("version") or 0) + 1,
        "previous": previous or None, "shared": ctx.role == "Client" or form.get("shared") == "true",
        "uploadedBy": ctx.actor["id"], "uploadedAt": now_iso(), "scanStatus": "Not scanned",
        **({"request": request["id"]} if request else {}),
    }
    try:
        with services.db.write() as session:
            repo = RecordRepository(session, ctx.owner)
            if parent:
                # The superseded version is marked first; a concurrent upload makes this fail (409).
                if not repo.update_if_revision(parent["id"], parent["revision"], {**parent, "supersededBy": document_id}, touch=False):
                    raise Conflict("The document changed during upload. Refresh and retry.")
            repo.insert(document_id, "document", data)
            AuditRepository.of(session, ctx).add(audit_label(ctx, "Uploaded document"), file.name)
            if request:
                # Linked in the same transaction: the request's ``evidence`` lists what was sent in
                # answer to it, for the reviewer. A decision taken meanwhile makes this fail (409).
                linked = {**request, "evidence": [*as_list(request.get("evidence")), document_id]}
                if not repo.update_if_revision(request["id"], request["revision"], linked):
                    raise Conflict("The request changed during upload. Refresh and retry.")
                AuditRepository.of(session, ctx).add(audit_label(ctx, "Attached document to request"), request.get("title"))
            if parent:
                # Work approved against the old version must be reviewed again.
                # Evidence always belongs to the record's own company (enforced when it is linked).
                repo.patch_where(REVIEW_ON_NEW_VERSION, {"reviewRequired": True}, where={"company": company}, evidence_contains=parent["id"])
    except BaseException:
        services.storage.delete(key)
        raise
    return {"ok": True, "id": document_id}


def download_document(services: Services, ctx: Context, document_id: str | None) -> tuple[dict, bytes]:
    doc = ctx.find(document_id, "document")
    if doc is None or not accessible(ctx, doc):
        raise NotFound("Document not found.")
    key = tenant_key(ctx.owner, doc.get("key"))
    content = services.storage.get(key) if key else None
    if content is None:
        raise NotFound("File bytes are unavailable in this local workspace.")
    return doc, content


# Review workspace: edited versions and version history -------------------------------------

# Formats the review workspace can produce an edited copy of (PDF annotations, spreadsheet edits).
EDITABLE_EXTENSIONS = ("pdf", "xlsx", "csv")
EDIT_KIND = {"pdf": "Annotated", "xlsx": "Spreadsheet edit", "csv": "Spreadsheet edit"}
MAX_CHAIN = 500


def _chain(ctx: Context, doc: dict) -> list[dict]:
    """Every version of ``doc``, oldest first, following ``previous`` back and ``supersededBy`` forward.

    Each link must stay within the document's company; a broken or foreign link ends the walk, so a
    tampered record can never pull in another company's file.
    """
    company, seen = doc.get("company"), {doc["id"]}
    chain = [doc]
    for direction, prepend in (("previous", True), ("supersededBy", False)):
        current = doc
        while len(chain) < MAX_CHAIN:
            nxt = ctx.find(current.get(direction), "document")
            if nxt is None or nxt.get("company") != company or nxt["id"] in seen:
                break
            seen.add(nxt["id"])
            chain.insert(0, nxt) if prepend else chain.append(nxt)
            current = nxt
    return chain


def _uploader(ctx: Context, member_id) -> str:
    if ctx.role == "Client":
        # Clients never receive staff profile details; they only learn whether the file was theirs.
        return "You" if member_id == ctx.actor.get("id") else "Your company secretary"
    if member_id == ctx.actor.get("id"):
        return ctx.actor.get("name") or "You"
    profile = ctx.find(member_id, "member")
    return (profile or {}).get("name") or (member_id if isinstance(member_id, str) and member_id else "Unknown")


def _request_of(ctx: Context, doc: dict) -> dict | None:
    request = ctx.find(doc.get("request"), "request") if doc.get("request") else None
    return request if request is not None and request.get("company") == doc.get("company") else None


def _edit_block_reason(ctx: Context, chain: list[dict], doc: dict) -> str | None:
    """Why ``doc`` cannot be edited by this actor, or None when it can."""
    if ctx.role not in EDIT_ROLES:
        return "Your role can view this document but not edit it."
    if _extension(text(doc.get("name"), 255)) not in EDITABLE_EXTENSIONS:
        return "This format can be previewed but not edited here."
    if chain[-1]["id"] != doc["id"]:
        return "A newer version exists. Open the latest version to edit."
    request = _request_of(ctx, doc)
    if request is not None and request.get("status") == "Closed":
        return "This request is closed. Its documents are read-only."
    return None


def document_versions(services: Services, ctx: Context, document_id: str | None) -> dict:
    """Version history of a document as the actor may see it (a client sees only shared versions)."""
    doc = ctx.find(document_id, "document")
    if doc is None or not accessible(ctx, doc):
        raise NotFound("Document not found.")
    chain = _chain(ctx, doc)
    visible = [d for d in chain if accessible(ctx, d)]
    root = chain[0]
    versions = [{
        "id": d["id"], "name": d.get("name"), "version": d.get("version") or 1, "size": d.get("size"),
        "hash": d.get("hash"), "shared": d.get("shared") is True, "current": d["id"] == chain[-1]["id"],
        "original": d["id"] == root["id"], "uploadedAt": d.get("uploadedAt") or d.get("updated"),
        "uploadedBy": _uploader(ctx, d.get("uploadedBy")), "editKind": d.get("editKind"),
        "editNote": d.get("editNote"), "category": d.get("category"), "period": d.get("period"),
        "revision": d.get("revision"),
    } for d in visible]
    request = _request_of(ctx, root) or _request_of(ctx, doc)
    if request is not None and not accessible(ctx, request):
        request = None
    latest = visible[-1] if visible else doc
    return {
        "document": doc["id"], "latest": latest["id"], "versions": versions,
        "request": {"id": request["id"], "title": request.get("title"), "status": request.get("status")} if request else None,
        "editable": _edit_block_reason(ctx, chain, latest) is None,
        "editBlocked": _edit_block_reason(ctx, chain, latest),
    }


def save_revision(services: Services, ctx: Context, form: dict, file: IncomingFile | None) -> dict:
    """Stores an edited copy of a document as its next version; the edited file never replaces the original.

    The new version keeps the parent's name, company, category, period and request link, records who
    saved it and when, and supersedes the parent exactly like an uploaded new version (dependent
    approvals are flagged for review). Saving does not submit or decide the request.
    """
    authorize(ctx, EDIT_ROLES)
    parent = ctx.find(_field(form, "id"), "document")
    if parent is None or not accessible(ctx, parent):
        raise NotFound("Document not found.")
    if _field(form, "revision") != str(parent.get("revision")):
        raise Conflict("This document changed. Reload it before saving.")
    reason = _edit_block_reason(ctx, _chain(ctx, parent), parent)
    if reason is not None or ctx.exists(kinds=["document"], where={"company": parent.get("company"), "previous": parent["id"]}):
        raise BadRequest(reason or "A newer version exists. Open the latest version to edit.")
    extension = _validate_file(file)
    assert file is not None
    expected = _extension(text(parent.get("name"), 255))
    if extension != expected:
        raise BadRequest(f"Save the edited document in its original format (.{expected}).")
    digest = hashlib.sha256(file.content).hexdigest()
    if digest == parent.get("hash"):
        raise BadRequest("No changes to save: the file is identical to the current version.")

    company = parent.get("company")
    document_id = str(uuid.uuid4())
    key = f"{ctx.owner}/{document_id}"
    request = _request_of(ctx, parent)
    data = {
        "category": parent.get("category") or "Other", "period": parent.get("period") or "", "name": parent.get("name"),
        "company": company, "key": key, "size": file.size, "hash": digest,
        "version": (parent.get("version") or 1) + 1, "previous": parent["id"],
        "shared": form.get("shared") == "true", "uploadedBy": ctx.actor["id"], "uploadedAt": now_iso(),
        "scanStatus": "Not scanned", "editKind": EDIT_KIND[extension], "editNote": _field(form, "note").strip()[:2000],
        **({"request": request["id"]} if request else {}),
    }
    services.storage.put(key, file.content)
    try:
        with services.db.write() as session:
            repo = RecordRepository(session, ctx.owner)
            if not repo.update_if_revision(parent["id"], parent["revision"], {**parent, "supersededBy": document_id}, touch=False):
                raise Conflict("This document changed. Reload it before saving.")
            repo.insert(document_id, "document", data)
            AuditRepository.of(session, ctx).add(audit_label(ctx, f"Saved edited version {data['version']}"), parent.get("name"))
            repo.patch_where(REVIEW_ON_NEW_VERSION, {"reviewRequired": True}, where={"company": company}, evidence_contains=parent["id"])
    except BaseException:
        services.storage.delete(key)
        raise
    return {"ok": True, "id": document_id, "version": data["version"]}


def document_share(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, ["Owner", "Secretary"])
    if r.get("kind") != "document":
        raise BadRequest("Select a document.")
    shared = truthy(body.get("shared"))
    return save(services, ctx, r, {**r, "shared": shared}, "Shared document with company portal" if shared else "Removed portal document access")


def import_trial_balance(services: Services, ctx: Context, form: dict, file: IncomingFile | None) -> dict:
    authorize(ctx, ["Owner", "Accountant", "Secretary"])
    company, period, currency = _field(form, "company"), _field(form, "period"), _field(form, "currency")
    if not _company_accessible(ctx, company):
        raise Forbidden("Company access required.")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period, re.ASCII) or currency not in CURRENCIES:
        raise BadRequest("Choose a valid period and currency.")
    if file is None or not file.name.endswith(".csv") or file.size > MAX_IMPORT_BYTES:
        raise BadRequest("Choose a CSV file up to 1 MB.")
    parsed = parse_trial_balance(file.content.decode("utf-8", errors="replace").removeprefix("﻿"))
    if not parsed["balanced"]:
        raise BadRequest(f"Debits and credits differ by {parsed['difference'] / 100:.2f} {currency}. Correct the file before importing.")
    digest = hashlib.sha256(file.content).hexdigest()
    # Newest first, so the first match is the latest import (same tie order as before).
    same_period = ctx.query(kinds=["trialbalance"], where={"company": company, "period": period, "currency": currency})
    if any(r.get("hash") == digest for r in same_period):
        raise BadRequest("This file has already been imported for this company, period and currency.")
    previous = same_period[0] if same_period else None

    import_id = str(uuid.uuid4())
    key = f"{ctx.owner}/{import_id}"
    services.storage.put(key, file.content)
    data = {
        "title": file.name, "company": company, "period": period, "currency": currency, **parsed, "key": key, "hash": digest,
        "importedBy": ctx.actor["id"], "previousImport": previous["id"] if previous else None,
        "version": ((previous or {}).get("version") or 0) + 1, "status": "Review required",
    }
    try:
        with services.db.write() as session:
            repo = RecordRepository(session, ctx.owner)
            repo.insert(import_id, "trialbalance", data)
            AuditRepository.of(session, ctx).add(audit_label(ctx, "Imported trial balance"), file.name)
            if previous:
                # A close linked to the previous import must be reviewed against the new version.
                repo.patch_where(("close",), {"reviewRequired": True}, where={"company": company, "trialbalance": previous["id"]})
    except BaseException:
        services.storage.delete(key)
        raise
    return {"ok": True, "id": import_id}


def review_import(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, ["Owner", "Secretary", "Accountant"])
    if r.get("kind") != "trialbalance":
        raise BadRequest("Choose a trial balance.")
    if r.get("importedBy") == ctx.actor["id"]:
        raise BadRequest("A different reviewer must review this import.")
    if ctx.exists(kinds=["trialbalance"], where={"company": r.get("company"), "previousImport": r["id"]}):
        raise BadRequest("A newer import exists. Review the latest version.")
    return save(services, ctx, r, {**r, "status": "Reviewed", "reviewedBy": ctx.actor["id"], "reviewedAt": now_iso()}, "Reviewed trial balance")
