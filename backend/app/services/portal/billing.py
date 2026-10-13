"""INR invoices and manually reported payments (ported from ``lib/portal-service.ts``).

Amounts are integer paise. Issued invoices are immutable; invoice numbers are unique among
issued/void invoices; payments are reported by the client and confirmed or rejected by staff.
"""
from __future__ import annotations

import uuid

from app.core.container import Services
from app.core.errors import BadRequest, Conflict
from app.core.jsvalues import now_iso, text, today
from app.domain.portal_domain import invoice_data, money
from app.domain.workflow_rules import require_date
from app.permissions import Context
from app.repositories.records import AuditRepository, RecordRepository
from app.services.context import audit_label
from app.services.portal.common import evidence, portal_save, record, writer


def _payments(ctx: Context, invoice: dict) -> list[dict]:
    """Payments of an invoice (payments always carry their invoice's company)."""
    return ctx.query(kinds=["payment"], where={"company": invoice.get("company"), "invoice": invoice.get("id")})


def _reference_taken(payments: list[dict], reference: str) -> bool:
    """``payments`` are the company's payments; references compare case-insensitively."""
    wanted = reference.lower()
    return any(str(p.get("reference") or "").lower() == wanted and p.get("status") != "Rejected" for p in payments)


def invoice_create(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    data = invoice_data(body.get("data"))
    invoice_id = str(uuid.uuid4())
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(invoice_id, "invoice", {
            **data, "company": c["id"], "status": "Draft", "title": data["number"] or "Draft invoice",
            "buyer": {"name": c.get("name"), "address": c.get("billingAddress") or c.get("address") or "", "gstin": c.get("gstin") or ""},
            "createdBy": ctx.actor["id"],
        })
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Drafted invoice"), c.get("name"))
    return {"ok": True, "id": invoice_id}


def invoice_issue(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    r = record(ctx, body, "invoice")
    if r.get("status") != "Draft":
        raise BadRequest("Only draft invoices can be issued.")
    if not r.get("number") or not (r.get("buyer") or {}).get("address"):
        raise BadRequest("Invoice number and client billing/registered address are required before issue.")
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        # Uniqueness is checked against fresh data inside the write transaction.
        taken = any(
            i.get("id") != r["id"] and i.get("status") in ("Issued", "Void")
            for i in repo.find(kinds=["invoice"], where={"number": r["number"]})
        )
        if taken or not repo.update_if_revision(r["id"], r["revision"], {**r, "status": "Issued", "issuedAt": now_iso()}):
            raise Conflict("Invoice number already issued or record changed.")
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Issued INR invoice"), r["number"])
    return {"ok": True}


def invoice_void(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    r = record(ctx, body, "invoice")
    if r.get("status") not in ("Draft", "Issued") or any(p.get("status") != "Rejected" for p in _payments(ctx, r)):
        raise BadRequest("Only invoices without pending or confirmed payments can be voided.")
    if not text(body.get("reason")):
        raise BadRequest("Record the reason for voiding.")
    return portal_save(services, ctx, r, {**r, "status": "Void", "voidReason": text(body.get("reason"))}, "Voided invoice")


def payment_report(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    """A client (or staff) reports an offline payment; it stays pending until staff confirm it."""
    cid = c["id"]
    inv = record(ctx, body, "invoice")
    if inv.get("status") != "Issued":
        raise BadRequest("Only issued invoices accept payment reports.")
    amount = money(body.get("amount"))
    reserved = sum(p.get("amount", 0) for p in _payments(ctx, inv) if p.get("status") != "Rejected")
    if amount <= 0 or amount > (inv.get("total") or 0) - reserved:
        raise BadRequest("Amount must be greater than zero and no more than the remaining amount after pending payments.")
    reference, paid_at = text(body.get("reference")), text(body.get("paidAt"))
    require_date(paid_at, "Payment date")
    if paid_at > today():
        raise BadRequest("Payment date cannot be in the future.")
    if not reference:
        raise BadRequest("Enter the bank / transaction reference.")
    if _reference_taken(ctx.query(kinds=["payment"], where={"company": cid}), reference):
        raise BadRequest("This transaction reference has already been reported.")
    ids = evidence(ctx, body.get("evidence"), cid)
    if not ids:
        raise BadRequest("Upload and attach payment proof first.")
    payment_id = str(uuid.uuid4())
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        if _reference_taken(repo.find(kinds=["payment"], where={"company": cid}), reference) or not repo.update_if_revision(
            inv["id"], inv["revision"], {**inv, "lastPaymentReport": now_iso()}
        ):
            raise Conflict("Invoice changed. Reload before reporting payment.")
        repo.insert(payment_id, "payment", {
            "company": cid, "invoice": inv["id"], "amount": amount, "currency": "INR", "reference": reference, "paidAt": paid_at,
            "evidence": ids, "status": "Pending verification", "reportedBy": ctx.actor["id"], "reportedName": ctx.actor.get("name"),
            "created": now_iso(),
        })
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Reported payment for verification"), inv.get("number"))
    return {"ok": True, "id": payment_id}


def payment_review(services: Services, ctx: Context, body: dict, c: dict) -> dict:
    writer(ctx)
    payment = record(ctx, body, "payment")
    if payment.get("status") != "Pending verification":
        raise BadRequest("This payment has already been reviewed.")
    status = body.get("status")
    if status not in ("Confirmed", "Rejected"):
        raise BadRequest("Choose a payment decision.")
    if status == "Rejected" and not text(body.get("note")):
        raise BadRequest("Explain the rejected payment.")
    if status == "Confirmed":
        evidence(ctx, payment.get("evidence"), c["id"])
        invoice = ctx.find(payment.get("invoice")) or {}
        if invoice.get("status") != "Issued":
            raise BadRequest("Invoice is not active.")
        paid = sum(p.get("amount", 0) for p in _payments(ctx, invoice) if p.get("status") == "Confirmed")
        if paid + payment.get("amount", 0) > (invoice.get("total") or 0):
            raise BadRequest("This would exceed the invoice total.")
    return portal_save(services, ctx, payment, {
        **payment, "status": status, "note": text(body.get("note")), "reviewedBy": ctx.actor["id"],
        "reviewedName": ctx.actor.get("name"), "reviewedAt": now_iso(),
    }, "Payment " + status.lower())
