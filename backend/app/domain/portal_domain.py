"""Client-portal rules ported from ``lib/portal-domain.ts``: India company profile, INR money, invoices."""
from __future__ import annotations

import re
from typing import Any

from app.core.errors import BadRequest
from app.core.jsvalues import MAX_SAFE_INTEGER, as_list, date_valid, is_integer, js_number, js_string, prop, text, truthy

DOCUMENT_CATEGORIES = [
    "Engagement & KYC", "Incorporation certificate", "MOA & AOA", "PAN & TAN", "Registered office",
    "Directors & shareholders", "Statutory registers", "Board & AGM records", "Financial statements",
    "Annual return", "Filing acknowledgements", "Tax & other registrations", "Invoices & payment proof", "Other",
]
INCORPORATION_SOURCE = "https://www.mca.gov.in/content/dam/mca/pdf/SPICEplus-and-linked-filings-FAQs-V3-20230122.pdf"
ACT_SOURCE = "https://www.indiacode.nic.in/indiacode/handle/123456789/2114?view_type=browse"
STAGES = [
    ("engagement", "Engagement & company information", "Confirm the service scope and submit the company profile."),
    ("verification", "Identity & document review", "Secretary reviews promoter, director and registered-office evidence."),
    ("constitution", "Name & incorporation preparation", "Prepare the applicable SPICe+ and constitutional documents."),
    ("registration", "Registration confirmation", "Record the actual registration identifiers and certificate."),
    ("postregistration", "Post-registration setup", "Review registers, bank/accounting setup and applicable registrations."),
    ("annual", "Annual compliance handover", "Agree the annual plan, responsibilities and reviewed due dates."),
]
ANNUAL_ITEMS = [
    "Financial statements & annual filing review",
    "Annual return preparation & filing review",
    "Board / AGM records and approvals",
    "Director disclosures & KYC applicability review",
    "Tax and other registration compliance review",
    "Event-based filings and statutory registers review",
]
PROFILE_FIELDS = [
    "name", "tradingName", "entityType", "registration", "incorporationDate", "email", "phone", "website", "address",
    "city", "state", "pincode", "businessActivity", "pan", "tan", "gstin", "yearEnd", "contact", "contactRole", "billingAddress",
]
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+\Z", re.ASCII)


def _match(pattern: str, value: str) -> bool:
    # JS semantics: ASCII-only \d and "$" that does not match before a trailing newline.
    return re.search(pattern.replace("$", r"\Z"), value, re.ASCII) is not None


def money(value: Any) -> int:
    """INR amount (max two decimals) converted to integer paise."""
    raw = ("" if value is None else js_string(value)).strip()
    if not _match(r"^\d+(\.\d{1,2})?$", raw):
        raise BadRequest("Amounts must be non-negative INR values with at most two decimals.")
    whole, _, fraction = raw.partition(".")
    amount = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if amount > MAX_SAFE_INTEGER or amount > 1e11:
        raise BadRequest("Amount exceeds the supported range.")
    return amount


def company_profile(data: Any, submit: bool = False) -> dict:
    data = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {"jurisdiction": "India", "currency": "INR"}
    for key in PROFILE_FIELDS:
        out[key] = text(data.get(key), 2000 if "ddress" in key or key == "businessActivity" else 300)
    if not out["name"]:
        raise BadRequest("Company / proposed company name is required.")
    if out["registration"] and out["entityType"] != "Other — secretary to confirm" and not _match(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$", out["registration"].upper()):
        raise BadRequest("Enter a 21-character CIN, or leave it blank for a proposed company.")
    if out["email"] and not EMAIL_RE.search(out["email"]):
        raise BadRequest("Enter a valid contact email.")
    if out["phone"] and not _match(r"^\+?[\d ()-]{7,20}$", out["phone"]):
        raise BadRequest("Enter a valid phone number.")
    if out["pincode"] and not _match(r"^\d{6}$", out["pincode"]):
        raise BadRequest("Indian PIN code must contain six digits.")
    if out["incorporationDate"] and not date_valid(out["incorporationDate"]):
        raise BadRequest("Enter a valid incorporation date.")
    if out["pan"] and not _match(r"^[A-Z]{5}\d{4}[A-Z]$", out["pan"].upper()):
        raise BadRequest("PAN must contain five letters, four digits and one letter.")
    if out["tan"] and not _match(r"^[A-Z]{4}\d{5}[A-Z]$", out["tan"].upper()):
        raise BadRequest("Enter a valid TAN format.")
    if out["gstin"] and not _match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$", out["gstin"].upper()):
        raise BadRequest("Enter a valid 15-character GSTIN format.")
    if out["website"] and not re.match(r"^https?://", out["website"], re.IGNORECASE):
        raise BadRequest("Website must start with https:// or http://.")
    out["directors"] = [
        {"name": text(prop(d, "name")), "din": text(prop(d, "din")), "designation": text(prop(d, "designation")), "email": text(prop(d, "email"))}
        for d in as_list(data.get("directors"))[:50]
    ]
    if any(not d["name"] or (d["din"] and not _match(r"^\d{8}$", d["din"])) or (d["email"] and not EMAIL_RE.search(d["email"])) for d in out["directors"]):
        raise BadRequest("Each director needs a name, a valid email if entered, and an eight-digit DIN if available.")
    out["shareholders"] = []
    for holder in as_list(data.get("shareholders"))[:100]:
        holding = prop(holder, "holding")
        out["shareholders"].append({"name": text(prop(holder, "name")), "holding": "" if holding is None else js_string(holding)})
    invalid = any(not s["name"] or not _match(r"^\d+(\.\d{1,4})?$", s["holding"]) or js_number(s["holding"]) > 100 for s in out["shareholders"])
    if invalid or sum(js_number(s["holding"]) for s in out["shareholders"]) > 100.00001:
        raise BadRequest("Shareholdings must be valid percentages totalling no more than 100%.")
    out["authorisedCapital"] = money(data.get("authorisedCapital")) if truthy(data.get("authorisedCapital")) else 0
    out["paidCapital"] = money(data.get("paidCapital")) if truthy(data.get("paidCapital")) else 0
    if out["paidCapital"] > out["authorisedCapital"]:
        raise BadRequest("Paid-up capital cannot exceed authorised capital.")
    if submit:
        if any(not out[k] for k in ("entityType", "email", "phone", "address", "city", "state", "pincode", "businessActivity", "contact")):
            raise BadRequest("Complete company type, contact, business activity and registered-office details before submission.")
        if not out["directors"]:
            raise BadRequest("Add at least one proposed or existing director.")
        if not truthy(data.get("confirmed")):
            raise BadRequest("Confirm that the supplied information is accurate.")
    return out


def invoice_data(data: Any) -> dict:
    data = data if isinstance(data, dict) else {}
    items = []
    for item in as_list(data.get("items"))[:50]:
        quantity = js_number(prop(item, "quantity"))
        if not text(prop(item, "description")) or not is_integer(quantity) or quantity < 1 or quantity > 10000:
            raise BadRequest("Invoice lines require a description and whole quantity from 1 to 10,000.")
        rate = money(prop(item, "rate"))
        items.append({"description": text(prop(item, "description")), "quantity": int(quantity), "rate": rate, "amount": int(quantity) * rate})
    if not items:
        raise BadRequest("Add at least one invoice line.")
    subtotal = sum(i["amount"] for i in items)
    tax = money(data.get("tax") if truthy(data.get("tax")) else "0")
    total = subtotal + tax
    if total > MAX_SAFE_INTEGER or total <= 0 or total > 1e11:
        raise BadRequest("Invoice total must be greater than zero and within the supported limit.")
    if not date_valid(data.get("due")):
        raise BadRequest("Enter a valid invoice due date.")
    seller_in = data.get("seller")
    seller = {
        "name": text(prop(seller_in, "name")),
        "address": text(prop(seller_in, "address"), 2000),
        "email": text(prop(seller_in, "email")),
        "gstin": text(prop(seller_in, "gstin")),
        "paymentInstructions": text(prop(seller_in, "paymentInstructions"), 2000),
    }
    if not seller["name"] or not seller["address"]:
        raise BadRequest("Enter the company secretary / firm name and billing address.")
    return {"items": items, "subtotal": subtotal, "tax": tax, "total": total, "currency": "INR", "due": data["due"],
            "seller": seller, "notes": text(data.get("notes"), 3000), "number": text(data.get("number"), 80)}
