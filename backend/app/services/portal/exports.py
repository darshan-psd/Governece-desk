"""Client-portal downloads: document ZIP, calendar (.ics), invoice and receipt HTML (from ``portalExport``)."""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass

from app.core.container import Services
from app.core.errors import BadRequest, NotFound
from app.core.jsvalues import now_iso, truthy
from app.domain.workflow_rules import escape_html
from app.permissions import Context
from app.services.portal.common import company
from app.services.portal.state import portal_state
from app.storage import tenant_key

MAX_BUNDLE_FILES = 200
MAX_BUNDLE_BYTES = 100 * 1024 * 1024


@dataclass
class Download:
    content: bytes | str
    media_type: str
    filename: str


def money_text(paise: int) -> str:
    """``INR 1,50,050.00`` (Indian digit grouping, like ``toLocaleString('en-IN')``)."""
    sign = "-" if paise < 0 else ""
    rupees, cents = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        digits = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head) + "," + tail
    return f"INR {sign}{digits}.{cents:02d}"


def _bundle(services: Services, ctx: Context, c: dict) -> Download:
    docs = [r for r in ctx.query(kinds=["document"], where={"company": c["id"]}) if truthy(r.get("shared"))]
    if not docs:
        raise BadRequest("No published documents are available to download.")
    if len(docs) > MAX_BUNDLE_FILES or sum(r.get("size") or 0 for r in docs) > MAX_BUNDLE_BYTES:
        raise BadRequest("This library exceeds the 100 MB / 200-file package limit. Download files individually.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for d in docs:
            key = tenant_key(ctx.owner, d.get("key"))
            content = services.storage.get(key) if key else None
            if content is None:
                raise BadRequest(f"A published file is unavailable: {d.get('name')}. Contact the secretary.")
            folder = re.sub(r"[^\w &-]", "_", d.get("category") or "Other", flags=re.ASCII)
            name = re.sub(r"[^\w. -]", "_", d.get("name") or "", flags=re.ASCII)
            archive.writestr(f"{folder}/{d['id']}-v{d.get('version') or 1}-{name}", content)
        manifest = {
            "company": c.get("name"),
            "generated": now_iso(),
            "scope": "All published documents, including historical versions. This is not a certificate of legal completeness.",
            "documents": [
                {"id": d["id"], "name": d.get("name"), "category": d.get("category") or "Other", "version": d.get("version") or 1,
                 "hash": d.get("hash"), "superseded": truthy(d.get("supersededBy"))}
                for d in docs
            ],
        }
        archive.writestr("document-manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return Download(buffer.getvalue(), "application/zip", "company-compliance-library.zip")


def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _calendar(state: dict, c: dict) -> Download:
    stamp = re.sub(r"[-:]", "", now_iso()).split(".")[0] + "Z"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Governance Desk//Client Calendar//EN", "CALSCALE:GREGORIAN"]
    for r in state["calendar"]:
        if r.get("company") != c["id"]:
            continue
        lines += [
            "BEGIN:VEVENT", f"UID:{r['id']}@governance.local", f"DTSTAMP:{stamp}", "DTSTART;VALUE=DATE:" + str(r.get("due")).replace("-", ""),
            "SUMMARY:" + _ics_escape(str(r.get("title"))),
            "DESCRIPTION:" + _ics_escape(f"{c.get('name')} · {r.get('type')} · Date recorded by your company secretary."),
            "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", "DESCRIPTION:Compliance reminder", "END:VALARM", "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return Download("\r\n".join(lines) + "\r\n", "text/calendar; charset=utf-8", "company-calendar.ics")


def _invoice_html(invoice: dict, payment: dict | None) -> str:
    e = escape_html
    seller, buyer = invoice.get("seller") or {}, invoice.get("buyer") or {}
    issued = (invoice.get("issuedAt") or "")[:10] or "Not issued"
    title = "Payment receipt" if payment else "Invoice"
    if payment:
        body = (
            f"<h2>{e(money_text(payment.get('amount', 0)))}</h2><p>Payment date: {e(payment.get('paidAt'))}<br>Reference: {e(payment.get('reference'))}"
            f"<br>Receipt: {e(payment.get('id'))}<br>Confirmed by: {e(payment.get('reviewedName'))} on {e(payment.get('reviewedAt'))}</p>"
        )
    else:
        rows = "".join(
            f"<tr><td>{e(i.get('description'))}</td><td>{i.get('quantity')}</td><td>{e(money_text(i.get('rate', 0)))}</td><td>{e(money_text(i.get('amount', 0)))}</td></tr>"
            for i in invoice.get("items") or []
        )
        body = (
            f"<table><thead><tr><th>Service</th><th>Qty</th><th>Rate</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p class=\"total\">Subtotal {e(money_text(invoice.get('subtotal', 0)))}<br>Tax (as entered) {e(money_text(invoice.get('tax', 0)))}<br>Total {e(money_text(invoice.get('total', 0)))}</p>"
            f"<p>Confirmed paid: {e(money_text(invoice.get('paid', 0)))} · Balance: {e(money_text(invoice.get('balance', 0)))}</p>"
            f"<h3>Payment instructions</h3><pre>{e(seller.get('paymentInstructions') or 'Contact your company secretary for payment instructions.')}</pre>"
            f"<pre>{e(invoice.get('notes'))}</pre>"
        )
    return (
        f'<!doctype html><html lang="en"><meta charset="utf-8"><title>{title} {e(invoice.get("number"))}</title>'
        "<style>body{font:15px/1.6 system-ui;color:#183344;max-width:860px;margin:45px auto;padding:20px}table{width:100%;border-collapse:collapse}"
        "th,td{padding:12px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap;font:inherit}.total{text-align:right;font-size:22px}"
        "@media print{body{margin:0}}</style>"
        f"<h1>{title} {e(invoice.get('number') or 'DRAFT')}</h1><p>{e(invoice.get('status'))} · Issued {e(issued)} · Due {e(invoice.get('due'))}</p>"
        f"<h2>{e(seller.get('name'))}</h2><pre>{e(seller.get('address'))}\n{e(seller.get('email'))}\n{e(seller.get('gstin'))}</pre>"
        f"<h3>Bill to: {e(buyer.get('name'))}</h3><pre>{e(buyer.get('address'))}\n{e(buyer.get('gstin'))}</pre>{body}"
        "<p>Local service record. Payment status is based on secretary verification; no online payment was processed by this application.</p></html>"
    )


def portal_export(services: Services, ctx: Context, params: dict) -> Download:
    mode = params.get("export")
    c = company(ctx, params.get("company"))
    # Only this company's slice of the portal state is needed for its calendar, invoices and receipts.
    state = portal_state(ctx, only_company=c["id"])
    if mode == "bundle":
        return _bundle(services, ctx, c)
    if mode == "calendar":
        return _calendar(state, c)
    item_id = params.get("id")
    invoice, payment = None, None
    if mode == "receipt":
        payment = next((p for p in state["payments"] if p["id"] == item_id and p.get("company") == c["id"] and p.get("status") == "Confirmed"), None)
        if payment is None:
            raise NotFound("Confirmed payment receipt not found.")
        invoice = next((i for i in state["invoices"] if i["id"] == payment.get("invoice")), None)
    elif mode == "invoice":
        invoice = next((i for i in state["invoices"] if i["id"] == item_id and i.get("company") == c["id"]), None)
    if invoice is None:
        raise NotFound("Invoice not found.")
    filename = "payment-receipt.html" if payment else "service-invoice.html"
    return Download(_invoice_html(invoice, payment), "text/html; charset=utf-8", filename)
