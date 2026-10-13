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
from app.core.jsvalues import now_iso, truthy
from app.domain.accounting import parse_trial_balance
from app.domain.portal_domain import DOCUMENT_CATEGORIES
from app.permissions import Context, accessible, authorize
from app.repositories.records import AuditRepository, RecordRepository
from app.services.context import audit_label, save
from app.storage import tenant_key

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = ("pdf", "txt", "csv", "xlsx", "docx")
CURRENCIES = ("INR", "GBP", "USD", "SGD", "EUR", "AUD")
REVIEW_ON_NEW_VERSION = ("task", "meeting", "close", "event", "submission")


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


def upload_document(services: Services, ctx: Context, form: dict, file: IncomingFile | None) -> dict:
    authorize(ctx, ["Owner", "Secretary", "Accountant", "Client", "Director"])
    company = _field(form, "company")
    if not _company_accessible(ctx, company):
        raise Forbidden("Company access is required.")
    if file is None or file.size == 0 or file.size > MAX_DOCUMENT_BYTES:
        raise BadRequest("Choose a file between 1 byte and 10 MB.")
    extension = file.name.split(".")[-1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise BadRequest("Supported formats: PDF, TXT, CSV, XLSX and DOCX.")
    start = file.content[:5]
    if extension == "pdf" and start.decode("utf-8", errors="replace") != "%PDF-":
        raise BadRequest("This file does not have a valid PDF header.")
    if extension in ("xlsx", "docx") and start[:2] != b"PK":
        raise BadRequest("This file does not have a valid Office document header.")

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
        "uploadedBy": ctx.actor["id"], "scanStatus": "Not scanned",
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
