"""Workflow rules ported from ``lib/workflow-rules.ts``: validation, dates, review gates, exports."""
from __future__ import annotations

import calendar
import re
import uuid
from datetime import date
from typing import Any

from app.core.errors import BadRequest
from app.core.jsvalues import as_list, date_valid, is_integer, js_number, js_string, prop, text, truthy

ROLES = ("Owner", "Secretary", "Accountant", "Director", "Client", "Auditor")
REVIEW_ROLES = ["Owner", "Secretary", "Director"]
EDIT_ROLES = ["Owner", "Secretary", "Accountant"]

EVENT_TEMPLATES: dict[str, list[str]] = {
    "Director appointment": ["Obtain consent and disclosures", "Review appointment resolution", "Update officer register", "Prepare applicable filing", "Record acknowledgement"],
    "Director resignation": ["Obtain resignation notice", "Record board acknowledgement", "Update officer register", "Review applicable filings"],
    "Registered office change": ["Collect address evidence", "Approve office change", "Update corporate records", "Review applicable filings"],
    "Share allotment": ["Review authority and terms", "Approve allotment resolution", "Update ownership register", "Review applicable filings"],
    "Registration renewal": ["Confirm renewal requirements", "Collect supporting documents", "Approve renewal submission", "Record renewal evidence"],
}


def require_date(value: Any, label: str = "Due date") -> str:
    if not date_valid(value):
        raise BadRequest(label + " must be a valid calendar date.")
    return value


def clean_checklist(value: Any) -> list[dict]:
    items = []
    for item in as_list(value)[:100]:
        entry = {"id": text(prop(item, "id")) or str(uuid.uuid4()), "label": text(prop(item, "label")), "done": truthy(prop(item, "done"))}
        if entry["label"]:
            items.append(entry)
    return items


def next_date(value: str, frequency: str, step: int = 1) -> str:
    require_date(value)
    current = date.fromisoformat(value)
    months = {"Monthly": 1, "Quarterly": 3, "Annual": 12}.get(frequency, 0)
    if not months:
        raise BadRequest("Choose Monthly, Quarterly or Annual.")
    total = current.year * 12 + (current.month - 1) + months * step
    year, month = divmod(total, 12)
    month += 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def can_access(member: dict, record: dict) -> bool:
    companies = member.get("companies")
    companies = companies if isinstance(companies, list) else []
    target = record.get("id") if record.get("kind") == "company" else record.get("company")
    return member.get("role") == "Owner" or "*" in companies or target in companies


def _strings(value: Any) -> list[str]:
    return [js_string(item) for item in as_list(value)]


def validate_record(kind: Any, data: Any) -> dict:
    data = data if isinstance(data, dict) else {}
    priority = data.get("priority")
    common = {
        "title": text(data.get("title")),
        "company": text(data.get("company")),
        "due": text(data.get("due")),
        "assignee": text(data.get("assignee")),
        "priority": priority if priority in ("Normal", "High", "Urgent") else "Normal",
        "notes": text(data.get("notes"), 12000),
    }
    if kind == "company":
        if not text(data.get("name")):
            raise BadRequest("Company name is required.")
        return {key: text(data.get(key)) for key in ("name", "jurisdiction", "registration", "yearEnd", "contact", "framework")}
    if not common["title"]:
        raise BadRequest("Title is required.")
    if not common["company"]:
        raise BadRequest("Choose a company.")
    require_date(common["due"])
    if kind in ("task", "close"):
        return {
            **common,
            "category": text(data.get("category")) or "Compliance",
            "checklist": clean_checklist(data.get("checklist")),
            "dependencies": _strings(data.get("dependencies")),
            "evidence": _strings(data.get("evidence")),
            "filing": truthy(data.get("filing")),
            "acknowledgement": text(data.get("acknowledgement")),
            "source": text(data.get("source")),
            "trialbalance": text(data.get("trialbalance")),
            "period": text(data.get("period")),
        }
    if kind == "meeting":
        quorum = js_number(data.get("quorum") if truthy(data.get("quorum")) else 1)
        if not is_integer(quorum) or quorum < 1 or quorum > 100:
            raise BadRequest("Quorum must be a whole number between 1 and 100.")
        attendees = []
        for item in as_list(data.get("attendees"))[:100]:
            entry = {"name": text(prop(item, "name")), "present": truthy(prop(item, "present")), "conflict": truthy(prop(item, "conflict"))}
            if entry["name"]:
                attendees.append(entry)
        return {
            **common,
            "location": text(data.get("location")),
            "quorum": int(quorum),
            "agenda": text(data.get("agenda"), 12000),
            "minutes": text(data.get("minutes"), 20000),
            "attendees": attendees,
            "evidence": _strings(data.get("evidence")),
        }
    if kind == "event":
        event_type = data.get("eventType")
        if not isinstance(event_type, str) or event_type not in EVENT_TEMPLATES:
            raise BadRequest("Select a supported corporate event.")
        return {**common, "eventType": event_type, "effective": require_date(data.get("effective"), "Effective date"), "evidence": _strings(data.get("evidence"))}
    if kind == "request":
        return {**common, "requestType": text(data.get("requestType")) or "Information", "response": "", "evidence": [], "status": "Open"}
    if kind == "rule":
        if data.get("frequency") not in ("Monthly", "Quarterly", "Annual"):
            raise BadRequest("Choose a recurrence.")
        if not text(data.get("source")):
            raise BadRequest("Record the source or internal policy reference.")
        offset = js_number(data.get("offset") if truthy(data.get("offset")) else 0)
        if not is_integer(offset) or offset < 0 or offset > 365:
            raise BadRequest("Deadline offset must be between 0 and 365 days.")
        return {
            **common,
            "jurisdiction": text(data.get("jurisdiction")),
            "source": text(data.get("source")),
            "frequency": data["frequency"],
            "offset": int(offset),
            "ruleVersion": 1,
            "status": "Draft",
            "scope": "Internal / manually reviewed",
        }
    if kind == "submission":
        return {**common, "task": text(data.get("task")), "method": "Manual", "reference": "", "evidence": [], "status": "Prepared"}
    raise BadRequest("Unsupported record type.")


def review_problems(record: dict, records: list[dict], actor: dict) -> list[str]:
    """Approval gate: every problem returned blocks the move to ``Approved``."""
    problems: list[str] = []
    doc_ids = as_list(record.get("evidence"))
    docs = [r for r in records if r.get("kind") == "document" and r.get("id") in doc_ids and r.get("company") == record.get("company")]
    distinct = []
    for doc_id in doc_ids:
        if doc_id not in distinct:
            distinct.append(doc_id)
    if len(docs) != len(distinct):
        problems.append("All evidence must belong to this company.")
    if not docs:
        problems.append("Attach at least one supporting document.")
    if any(any(n.get("kind") == "document" and n.get("previous") == d.get("id") for n in records) for d in docs):
        problems.append("Replace superseded evidence with the latest document version.")
    if (record.get("preparer") or "owner") == actor.get("id"):
        problems.append("A different team member must approve the work.")
    by_id = {r.get("id"): r for r in records}
    if any((by_id.get(dep) or {}).get("status") != "Completed" for dep in as_list(record.get("dependencies"))):
        problems.append("Complete all dependent tasks first.")
    if any(not truthy(prop(item, "done")) for item in as_list(record.get("checklist"))):
        problems.append("Complete every preparation checklist item.")
    if record.get("kind") == "meeting":
        eligible = {text(prop(a, "name")).lower() for a in as_list(record.get("attendees")) if truthy(prop(a, "present")) and not truthy(prop(a, "conflict"))}
        if len(eligible) < (record.get("quorum") or 1):
            problems.append("The configured quorum is not met after excluding declared conflicts.")
        if not text(record.get("minutes")):
            problems.append("Record the meeting minutes.")
        if not text(record.get("agenda")):
            problems.append("Record the meeting agenda.")
    if record.get("kind") == "close":
        tb = next((r for r in records if r.get("id") == record.get("trialbalance") and r.get("company") == record.get("company")), None)
        if not tb or tb.get("kind") != "trialbalance" or tb.get("status") != "Reviewed":
            problems.append("Link a reviewed trial balance for this company.")
        elif any(r.get("previousImport") == tb.get("id") for r in records):
            problems.append("Link the latest trial balance version.")
        elif record.get("period") and record.get("period") != tb.get("period"):
            problems.append("The close period must match the trial balance period.")
        if any(r.get("kind") == "request" and r.get("company") == record.get("company") and r.get("status") != "Closed" for r in records):
            problems.append("Resolve outstanding company information requests.")
    return problems


_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def escape_html(value: Any) -> str:
    raw = "" if value is None else js_string(value)
    return re.sub(r"[&<>\"']", lambda m: _HTML_ESCAPES[m.group(0)], raw)


def csv_cell(value: Any) -> str:
    """Formula-safe CSV cell: values starting with = + @ - tab or CR are prefixed with '."""
    cell = "" if value is None else js_string(value)
    if re.match(r"^[=+@\-\t\r]", cell):
        cell = "'" + cell
    return '"' + cell.replace('"', '""') + '"'
