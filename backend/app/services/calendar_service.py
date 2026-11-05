"""Compliance calendar templates and bulk calendar setup.

A template is a reusable list of compliance activities: name, category, frequency, due-date rule,
responsible role, reminders and preparation instructions. Saving a template never creates calendar
entries. Applying one plans, for every selected company and every period of the chosen financial
year, one calendar entry. Each entry is an ordinary ``task`` record, the same kind the Calendar,
My work, reminders, reports and approvals already use, so every company's entries are independent
records managed through the existing workflow. Changing one company's entry, or the template,
changes nothing else.

Safety properties:

* Deterministic ids (``tpl-<template>-<company>-<activity>-<period>``) with insert-or-ignore:
  applying the same template again, or retrying after a partial failure, never duplicates an entry.
* Equivalent obligations are reused, not duplicated. An entry is skipped when the company already
  has a task with the same title due in the same month (any origin), and an activity is left to
  the Rule Library for a company where a *published* rule with the same title exists: published
  rules stay the source of rule-based obligations.
* Dates are proposals, not authoritative statutory deadlines. An entry whose activity requires
  verification is created "to be verified" unless the person confirmed that company's date in
  the preview, and can be confirmed later from the entry itself (``confirm_due``).
* The server always recomputes the plan from the stored template; the client never sends entries.
  An apply is refused when the template changed after the preview.
* Each company is written in its own transaction. One company failing leaves the others created,
  is reported and audited, and the run can be retried safely.

Records: ``calendartemplate`` is workspace configuration with no company, so only the Owner and
staff with access to every company can read it (``can_access``). ``calendarbatch`` is the log of
one bulk run (who started it, which companies, what each company's outcome was).
"""
from __future__ import annotations

import calendar as month_calendar
import logging
import re
import uuid
from datetime import date
from typing import Any

from app.core.container import Services
from app.core.errors import ApiError, BadRequest, Conflict, Forbidden, NotFound
from app.core.jsvalues import as_list, date_valid, is_integer, js_number, now_iso, text, today, unique
from app.domain.workflow_rules import CANCELLED, EDIT_ROLES, can_access
from app.permissions import Context, accessible, authorize
from app.repositories.records import AuditRepository, RecordRepository, field
from app.services.context import CHANGED, audit_label, save

log = logging.getLogger("governance.calendar")

TEMPLATE_KIND = "calendartemplate"
BATCH_KIND = "calendarbatch"
# The company types offered by the company profile; "" means the template applies to any type.
COMPANY_TYPES = ("Private limited company", "One Person Company", "Public limited company", "Section 8 company",
                 "Other — secretary to confirm")
# Months per period. Financial years run April to March (the Indian financial year).
FREQUENCIES = {"Monthly": 1, "Quarterly": 3, "Half-yearly": 6, "Annual": 12}
RESPONSIBLE_ROLES = ("Secretary", "Accountant", "Owner", "Director")
REMINDER_DAYS = (30, 15, 7, 3, 1, 0)
DEFAULT_REMINDERS = {"days": [7, 3, 1], "overdue": True}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MAX_ACTIVITIES = 40
MAX_COMPANIES = 200
MAX_ENTRIES = 6000
# Template management is the Owner's. Applying a template is the Owner's, and a secretary's only for
# a template the Owner has explicitly marked "secretaries may apply".
MANAGE_ROLES = ["Owner"]
APPLY_ROLES = ["Owner", "Secretary"]
RUN_ID = re.compile(r"[A-Za-z0-9-]{8,64}", re.ASCII)
# Client-facing note on an entry published to the client portal calendar.
PROVISIONAL_NOTE = "Provisional date: your company secretary will confirm it."


# Validation ---------------------------------------------------------------------------------

def financial_year(value: Any) -> str:
    fy = text(value, 20)
    if not re.fullmatch(r"20\d{2}-\d{2}", fy, re.ASCII) or int(fy[-2:]) != (int(fy[:4]) + 1) % 100:
        raise BadRequest("Use a financial year such as 2026-27.")
    return fy


def _normal(value: Any) -> str:
    """Case- and whitespace-insensitive form of a title, for equivalence checks."""
    return re.sub(r"\s+", " ", text(value, 300)).casefold()


def _whole(value: Any, low: int, high: int, message: str) -> int:
    if isinstance(value, bool) or value is None or value == "":
        raise BadRequest(message)
    number = js_number(value)
    if not is_integer(number) or not low <= number <= high:
        raise BadRequest(message)
    return int(number)


def clean_reminders(value: Any) -> dict:
    if not isinstance(value, dict):
        return {"days": list(DEFAULT_REMINDERS["days"]), "overdue": DEFAULT_REMINDERS["overdue"]}
    days = {d for d in as_list(value.get("days")) if isinstance(d, int) and not isinstance(d, bool) and d in REMINDER_DAYS}
    return {"days": sorted(days, reverse=True), "overdue": value.get("overdue") is True}


def clean_activity(item: Any, known: set) -> dict:
    item = item if isinstance(item, dict) else {}
    name = text(item.get("name"), 200)
    if not name:
        raise BadRequest("Give every compliance activity a name.")
    frequency = item.get("frequency")
    if frequency not in FREQUENCIES:
        raise BadRequest(f"Choose how often “{name}” recurs: Monthly, Quarterly, Half-yearly or Annual.")
    role = item.get("role") or "Secretary"
    if role not in RESPONSIBLE_ROLES:
        raise BadRequest(f"Choose the responsible role for “{name}”.")
    # An activity keeps its id across edits (generated entries reference it); new ones get a new id.
    activity_id = item.get("id") if isinstance(item.get("id"), str) and item.get("id") in known else "a" + uuid.uuid4().hex[:12]
    return {
        "id": activity_id,
        "name": name,
        "category": text(item.get("category"), 120) or "Compliance",
        "frequency": frequency,
        "dueDay": _whole(item.get("dueDay"), 1, 31, f"The due day for “{name}” must be a day of the month (1–31)."),
        "monthsAfter": _whole(item.get("monthsAfter", 0), 0, 12, f"Months after the period for “{name}” must be between 0 and 12."),
        "role": role,
        "reminders": clean_reminders(item.get("reminders")),
        "instructions": text(item.get("instructions"), 4000),
        "dateBasis": text(item.get("dateBasis"), 500),
        # Unless the template says the date is an internal deadline, each company's date must be verified.
        "verifyDates": item.get("verifyDates") is not False,
        "filing": item.get("filing") is True,
        "clientVisible": item.get("clientVisible") is True,
    }


def clean_template(data: Any, existing: dict | None = None) -> dict:
    data = data if isinstance(data, dict) else {}
    name = text(data.get("name"), 200)
    if not name:
        raise BadRequest("Give the template a name.")
    company_type = text(data.get("companyType"), 120)
    if company_type and company_type not in COMPANY_TYPES:
        raise BadRequest("Choose a company type from the list, or “Any company type”.")
    raw = as_list(data.get("activities"))
    if not raw:
        raise BadRequest("Add at least one compliance activity.")
    if len(raw) > MAX_ACTIVITIES:
        raise BadRequest(f"A template can hold at most {MAX_ACTIVITIES} activities.")
    known = {a.get("id") for a in as_list((existing or {}).get("activities")) if isinstance(a, dict)}
    activities, names = [], set()
    for item in raw:
        activity = clean_activity(item, known)
        if _normal(activity["name"]) in names:
            raise BadRequest(f"“{activity['name']}” is listed twice. Give every activity a distinct name.")
        names.add(_normal(activity["name"]))
        known.discard(activity["id"])  # an existing id can be kept by one activity only
        activities.append(activity)
    return {
        "name": name, "title": name, "description": text(data.get("description"), 2000), "companyType": company_type,
        "financialYear": financial_year(data.get("financialYear")), "secretaryApply": data.get("secretaryApply") is True,
        "activities": activities,
    }


def _ids(value: Any, label: str = "companies") -> list[str]:
    ids = unique([i for i in as_list(value) if isinstance(i, str) and i])
    if len(ids) > 500:
        raise BadRequest(f"Too many {label} in one request.")
    return ids


def _keyed(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# Template management (Owner) ----------------------------------------------------------------

def _template(r: dict) -> None:
    if r.get("kind") != TEMPLATE_KIND:
        raise BadRequest("Select a calendar template.")


def template_create(services: Services, ctx: Context, body: dict) -> dict:
    authorize(ctx, MANAGE_ROLES)
    data = clean_template(body.get("data"))
    template_id, now = str(uuid.uuid4()), now_iso()
    record = {**data, "status": "Active", "templateVersion": 1, "createdBy": ctx.actor["id"], "createdAt": now,
              "updatedBy": ctx.actor["id"], "updatedAt": now}
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(template_id, TEMPLATE_KIND, record)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Created calendar template"), data["name"])
    return {"ok": True, "id": template_id}


def template_save(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    """A new version of the template. Entries generated earlier keep the version they came from."""
    authorize(ctx, MANAGE_ROLES)
    _template(r)
    if r.get("status") == "Archived":
        raise BadRequest("Restore this template before editing it.")
    data = clean_template(body.get("data"), r)
    version = (r.get("templateVersion") or 1) + 1
    return save(services, ctx, r, {**r, **data, "templateVersion": version, "updatedBy": ctx.actor["id"], "updatedAt": now_iso()},
                f"Updated calendar template (version {version})", subject=data["name"])


def template_duplicate(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    authorize(ctx, MANAGE_ROLES)
    _template(r)
    name = text(body.get("name"), 200) or (text(r.get("name"), 190) + " (copy)")
    # A copy is a new template: secretaries must be permitted to apply it explicitly again.
    data = clean_template({**{k: r.get(k) for k in ("description", "companyType", "financialYear", "activities")}, "name": name}, r)
    template_id, now = str(uuid.uuid4()), now_iso()
    record = {**data, "status": "Active", "templateVersion": 1, "createdBy": ctx.actor["id"], "createdAt": now,
              "updatedBy": ctx.actor["id"], "updatedAt": now,
              "duplicatedFrom": {"id": r["id"], "name": r.get("name"), "version": r.get("templateVersion") or 1}}
    with services.db.write() as session:
        RecordRepository(session, ctx.owner).insert(template_id, TEMPLATE_KIND, record)
        AuditRepository.of(session, ctx).add(audit_label(ctx, "Duplicated calendar template"), f"{r.get('name')} → {name}")
    return {"ok": True, "id": template_id}


def template_archive(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    """Archives (or restores) a template. Archiving only stops it being applied: entries already
    generated from it are unchanged and keep their reference to it."""
    authorize(ctx, MANAGE_ROLES)
    _template(r)
    archive = body.get("archived") is not False
    if archive == (r.get("status") == "Archived"):
        raise BadRequest("This template already has that status.")
    now = now_iso()
    if archive:
        data, action = {**r, "status": "Archived", "archivedBy": ctx.actor["id"], "archivedAt": now}, "Archived calendar template"
    else:
        data = {**r, "status": "Active", "archivedBy": None, "archivedAt": None, "restoredBy": ctx.actor["id"], "restoredAt": now}
        action = "Restored calendar template"
    return save(services, ctx, r, data, action, subject=r.get("name"))


# Planning -----------------------------------------------------------------------------------

def periods(frequency: str, fy: str) -> list[dict]:
    """The periods of one financial year (April to March) for a frequency."""
    size = FREQUENCIES[frequency]
    first = int(fy[:4]) * 12 + 3  # April of the first calendar year, as a month index
    out = []
    for k in range(12 // size):
        start_index, end_index = first + k * size, first + k * size + size - 1
        sy, sm = divmod(start_index, 12)
        ey, em = divmod(end_index, 12)
        if size == 1:
            key, label = f"{sy}-{sm + 1:02d}", f"{MONTHS[sm]} {sy}"
        elif size == 12:
            key, label = fy, f"FY {fy}"
        else:
            prefix = "Q" if size == 3 else "H"
            span = f"{MONTHS[sm]}–{MONTHS[em]} {ey}" if sy == ey else f"{MONTHS[sm]} {sy}–{MONTHS[em]} {ey}"
            key, label = f"{fy}-{prefix}{k + 1}", f"{prefix}{k + 1} FY {fy} ({span})"
        out.append({"key": key, "label": label, "start": date(sy, sm + 1, 1).isoformat(),
                    "end": date(ey, em + 1, month_calendar.monthrange(ey, em + 1)[1]).isoformat(), "endIndex": end_index})
    return out


def due_for(end_index: int, activity: dict) -> str:
    """Day ``dueDay`` of the month ``monthsAfter`` months after the period's last month (clamped to month end)."""
    year, month = divmod(end_index + activity["monthsAfter"], 12)
    month += 1
    return date(year, month, min(activity["dueDay"], month_calendar.monthrange(year, month)[1])).isoformat()


def describe_rule(activity: dict) -> str:
    after = activity["monthsAfter"]
    when = "in the period's last month" if after == 0 else ("in the month after the period ends" if after == 1 else f"{after} months after the period ends")
    return f"{activity['frequency']} · due on day {activity['dueDay']} {when}"


def _member_name(member: dict) -> str:
    return text(member.get("name"), 200) or member["id"]


def _staff_members(ctx: Context) -> list[dict]:
    """Active members who may be responsible for internal work (never a client)."""
    members = [m for m in ctx.query(kinds=["member"]) if m.get("active") is not False and m.get("role") != "Client"]
    return sorted(members, key=lambda m: (_member_name(m).casefold(), m["id"]))


def _company_checks(template: dict, company: dict) -> tuple[bool, str, list[str]]:
    """Whether the template may be applied to the company, why not, and what to double-check."""
    if company.get("inactive") is True:
        return False, "Inactive company", []
    wanted, actual = text(template.get("companyType")), text(company.get("entityType"))
    if wanted and actual and wanted != actual:
        return False, f"Company type is “{actual}”; this template is for “{wanted}”.", []
    warnings = []
    if wanted and not actual:
        warnings.append("Company type not recorded: confirm this template applies to the company.")
    year_end = text(company.get("yearEnd"))
    if year_end and not (re.search(r"mar|\b0?3\b", year_end.casefold()) and "31" in year_end):
        warnings.append(f"Financial year end recorded as “{year_end}”; the template follows April–March. Verify the annual dates.")
    return True, "", warnings


def _origin(task: dict) -> str:
    if isinstance(task.get("template"), dict):
        return "calendar template “" + text(task["template"].get("name"), 200) + "”"
    if isinstance(task.get("rule"), dict):
        return "Rule Library"
    return "created individually"


def build_plan(ctx: Context, template: dict, body: dict, company_ids: list[str]) -> dict:
    """The complete, server-computed plan for ``company_ids``; nothing is written."""
    fy = financial_year(body.get("financialYear") or template.get("financialYear"))
    day = today()
    include_past = body.get("includePast") is True
    activities = [a for a in as_list(template.get("activities")) if isinstance(a, dict) and a.get("frequency") in FREQUENCIES]
    chosen = body.get("activities")
    included = {a["id"] for a in activities} if chosen is None else set(_ids(chosen, "activities"))
    active = [a for a in activities if a["id"] in included]
    if not active:
        raise BadRequest("Include at least one compliance activity.")
    if len(company_ids) > MAX_COMPANIES:
        raise BadRequest(f"Select at most {MAX_COMPANIES} companies at a time.")
    total = len(company_ids) * sum(12 // FREQUENCIES[a["frequency"]] for a in active)
    if total > MAX_ENTRIES:
        raise BadRequest(f"This setup would plan {total} entries. Select fewer companies or activities (limit {MAX_ENTRIES}).")
    found = ctx.find_many(company_ids, "company")
    if any(c not in found or not can_access(ctx.actor, found[c]) for c in company_ids):
        raise NotFound("Company not found.")

    members = _staff_members(ctx)
    version = template.get("templateVersion") or 1
    assignments_in, dates_in = _keyed(body.get("assignments")), _keyed(body.get("dates"))
    confirmed_in, excluded_in = _keyed(body.get("confirmed")), _keyed(body.get("excluded"))
    schedule = {a["id"]: periods(a["frequency"], fy) for a in active}

    # 1. Raw entries with the company's own date (an override replaces the template's date).
    companies, raw = [], []
    for cid in company_ids:
        company = found[cid]
        eligible, reason, warnings = _company_checks(template, company)
        dates = _keyed(dates_in.get(cid))
        confirmed = {k for k in as_list(confirmed_in.get(cid)) if isinstance(k, str)}
        excluded = {k for k in as_list(excluded_in.get(cid)) if isinstance(k, str)}
        chosen_people = _keyed(assignments_in.get(cid))
        assignable = [m for m in members if can_access(m, {"kind": "task", "company": cid})]
        assignments, problems = {}, []
        for role in unique([a["role"] for a in active]):
            if role in chosen_people:
                person = chosen_people[role]
                if person == "":
                    assignments[role] = ""
                elif isinstance(person, str) and any(m["id"] == person for m in assignable):
                    assignments[role] = person
                else:
                    assignments[role] = ""
                    problems.append(f"The person chosen for {role} activities can no longer be assigned work in {company.get('name')}.")
            else:
                assignments[role] = next((m["id"] for m in assignable if m.get("role") == role), "")
                if not assignments[role] and eligible:
                    warnings.append(f"No active {role.lower()} has access to this company: {role} activities stay unassigned unless you choose someone.")
        entry_list = []
        if eligible:
            for activity in active:
                for period in schedule[activity["id"]]:
                    key = f"{activity['id']}:{period['key']}"
                    generated = due_for(period["endIndex"], activity)
                    override = dates.get(key)
                    if override in (None, ""):
                        due = generated
                    elif date_valid(override):
                        due = override
                    else:
                        raise BadRequest(f"Enter a valid due date for {activity['name']} ({period['label']}) at {company.get('name')}.")
                    entry = {
                        "key": key, "id": f"tpl-{template['id']}-{cid}-{activity['id']}-{period['key']}", "activity": activity["id"],
                        "title": activity["name"], "category": activity["category"], "frequency": activity["frequency"],
                        "period": period["key"], "periodLabel": period["label"], "due": due, "generatedDue": generated,
                        "adjusted": due != generated, "verify": activity["verifyDates"],
                        "confirmed": (not activity["verifyDates"]) or key in confirmed,
                        "assignee": assignments.get(activity["role"], ""), "role": activity["role"],
                        "clientVisible": activity["clientVisible"], "filing": activity["filing"],
                        "status": "new", "reason": "", "existing": None, "excludedByUser": key in excluded,
                    }
                    entry_list.append(entry)
                    raw.append((cid, entry))
        companies.append({"id": cid, "name": company.get("name"), "type": text(company.get("entityType")),
                          "inactive": company.get("inactive") is True, "eligible": eligible, "reason": reason,
                          "warnings": warnings, "assignmentErrors": problems, "assignments": assignments,
                          "assignable": [{"id": m["id"], "name": _member_name(m)} for m in assignable],
                          "entries": entry_list})

    # 2. Existing work that could already cover an entry: one batched id lookup, one indexed query.
    by_id = ctx.find_many((e["id"] for _, e in raw), "task")
    equivalents: dict[tuple, list[dict]] = {}
    if raw:
        dues = [e["due"] for _, e in raw]
        nearby = ctx.query(kinds=["task"], where={"company": sorted({c for c, _ in raw})},
                           extra=[field("due") >= min(dues)[:7] + "-01", field("due") <= max(dues)[:7] + "-31"])
        for task in nearby:
            if task.get("status") == CANCELLED:
                continue  # cancelled work is history: it does not stand in for the obligation
            origin = task.get("template") if isinstance(task.get("template"), dict) else {}
            if origin.get("id") == template["id"]:
                continue  # this template's own entries are matched by id (their dates may have been moved)
            equivalents.setdefault((task.get("company"), _normal(task.get("title")), text(task.get("due"))[:7]), []).append(task)
    rules: dict[tuple, list[dict]] = {}
    company_rules: dict[str, list[dict]] = {}
    for rule in (ctx.query(kinds=["rule"], where={"company": sorted(company_ids)}) if raw else []):
        rules.setdefault((rule.get("company"), _normal(rule.get("title"))), []).append(rule)
        company_rules.setdefault(rule.get("company"), []).append(rule)

    # 3. Status of every entry, in order of precedence.
    for cid, e in raw:
        existing = by_id.get(e["id"])
        matching_rules = rules.get((cid, _normal(e["title"])), [])
        published = next((r for r in matching_rules if r.get("status") == "Published"), None)
        equivalent = next(iter(equivalents.get((cid, _normal(e["title"]), e["due"][:7]), [])), None)
        if existing is not None:
            e["status"], e["reason"] = "exists", f"Already created from this template ({existing.get('status') or 'Not started'}, due {existing.get('due')})"
            e["existing"] = {"id": existing["id"], "status": existing.get("status"), "due": existing.get("due")}
        elif e["excludedByUser"]:
            e["status"], e["reason"] = "excluded", "Excluded for this company"
        elif published is not None:
            e["status"] = "rule"
            e["reason"] = (f"Covered by the published Rule Library rule “{published.get('title')}” "
                           f"({published.get('frequency')}, version {published.get('ruleVersion') or 1}); the rule stays the source")
        elif equivalent is not None:
            e["status"] = "duplicate"
            e["reason"] = f"Equivalent obligation already scheduled: “{equivalent.get('title')}” due {equivalent.get('due')} ({_origin(equivalent)})"
            e["existing"] = {"id": equivalent["id"], "status": equivalent.get("status"), "due": equivalent.get("due")}
        elif e["due"] < day and not include_past:
            e["status"], e["reason"] = "past", "Due date has already passed"
        e.pop("excludedByUser", None)
    activity_names = {_normal(a["name"]) for a in active}
    for company in companies:
        published = {_normal(r.get("title")) for r in company_rules.get(company["id"], []) if r.get("status") == "Published"}
        drafts = sorted({text(r.get("title"), 200) for r in company_rules.get(company["id"], [])
                         if r.get("status") != "Published" and _normal(r.get("title")) in activity_names - published})
        for title in drafts:
            company["warnings"].append(f"Rule Library rule “{title}” is not published yet. Publishing and generating it later "
                                       "may duplicate these entries.")
        company["counts"] = _counts(company["entries"])

    entries = [e for c in companies for e in c["entries"]]
    counts = _counts(entries)
    return {
        "template": {"id": template["id"], "name": template.get("name"), "version": version, "companyType": text(template.get("companyType")),
                     "financialYear": text(template.get("financialYear"))},
        "financialYear": fy,
        "range": {"start": f"{fy[:4]}-04-01", "end": f"{int(fy[:4]) + 1}-03-31"},
        "today": day,
        "includePast": include_past,
        "activities": [{"id": a["id"], "name": a["name"], "category": a["category"], "frequency": a["frequency"], "role": a["role"],
                        "rule": describe_rule(a), "verifyDates": a["verifyDates"], "clientVisible": a["clientVisible"],
                        "included": a["id"] in included} for a in activities],
        "companies": companies,
        "summary": {
            "companies": len(companies), "eligibleCompanies": sum(1 for c in companies if c["eligible"]),
            "template": template.get("name"), "templateVersion": version,
            "activities": len(active), "activitiesTotal": len(activities),
            "entries": counts["new"] + counts["existing"] + counts["duplicates"],
            "existing": counts["existing"], "duplicates": counts["duplicates"], "ruleCovered": counts["ruleCovered"],
            "new": counts["new"], "needsConfirmation": counts["needsConfirmation"], "excluded": counts["excluded"], "past": counts["past"],
        },
    }


def _counts(entries: list[dict]) -> dict:
    statuses = [e["status"] for e in entries]
    rule = statuses.count("rule")
    return {
        "new": statuses.count("new"), "existing": statuses.count("exists"),
        "duplicates": statuses.count("duplicate") + rule, "ruleCovered": rule,
        "excluded": statuses.count("excluded"), "past": statuses.count("past"),
        "needsConfirmation": sum(1 for e in entries if e["status"] == "new" and not e["confirmed"]),
    }


# Preview and bulk creation --------------------------------------------------------------------

def _load_template(ctx: Context, body: dict) -> dict:
    authorize(ctx, APPLY_ROLES)
    template = ctx.find(body.get("template"), TEMPLATE_KIND)
    if template is None or not accessible(ctx, template):
        raise NotFound("Template not found.")
    if ctx.role != "Owner" and template.get("secretaryApply") is not True:
        raise Forbidden("The workspace owner has not permitted secretaries to apply this template.")
    if template.get("status") != "Active":
        raise BadRequest("Restore this template before applying it.")
    return template


def calendar_preview(services: Services, ctx: Context, body: dict) -> dict:
    template = _load_template(ctx, body)
    company_ids = _ids(body.get("companies"))
    if not company_ids:
        raise BadRequest("Select at least one company.")
    return {"ok": True, **build_plan(ctx, template, body, company_ids)}


def _task_record(ctx: Context, template: dict, plan: dict, company: dict, entry: dict, activity: dict, batch_id: str, now: str) -> dict:
    confirmed_now = entry["confirmed"] and activity["verifyDates"]
    return {
        "title": entry["title"], "company": company["id"], "due": entry["due"], "assignee": entry["assignee"], "priority": "Normal",
        "status": "Not started", "category": entry["category"],
        "source": activity["dateBasis"] or f"Compliance calendar template “{template.get('name')}”",
        "notes": activity["instructions"], "period": entry["periodLabel"], "filing": activity["filing"],
        "checklist": [], "dependencies": [], "evidence": [], "acknowledgement": "", "trialbalance": "",
        "preparer": ctx.actor["id"], "created": now, "reminders": activity["reminders"],
        "template": {
            "id": template["id"], "name": template.get("name"), "version": plan["template"]["version"], "activity": activity["id"],
            "period": entry["period"], "periodLabel": entry["periodLabel"], "financialYear": plan["financialYear"], "batch": batch_id,
            "generatedDue": entry["generatedDue"], "adjusted": entry["adjusted"], "verify": activity["verifyDates"],
            "dateConfirmed": entry["confirmed"], "dateBasis": activity["dateBasis"], "createdBy": ctx.actor["id"], "createdAt": now,
            **({"confirmedBy": ctx.actor["id"], "confirmedAt": now, "confirmedDue": entry["due"]} if confirmed_now else {}),
        },
    }


def _portal_record(template: dict, company: dict, entry: dict, activity: dict) -> dict:
    """Client-visible mirror (the existing ``publishTask`` shape): title, date, period and status only."""
    return {
        "company": company["id"], "title": entry["title"], "sourceTask": entry["id"], "due": entry["due"], "status": "Not started",
        "category": entry["category"], "period": entry["periodLabel"],
        "source": activity["dateBasis"] or "Company secretary compliance calendar",
        "note": "" if entry["confirmed"] else PROVISIONAL_NOTE, "evidence": [], "calendarTemplate": template["id"],
    }


def _audit(services: Services, ctx: Context, action: str, subject: str) -> None:
    with services.db.write() as session:
        AuditRepository.of(session, ctx).add(audit_label(ctx, action), subject)


def _apply_company(services: Services, ctx: Context, template: dict, plan: dict, company: dict, batch_id: str) -> dict:
    """Creates one company's new entries in one transaction (all of them, or none)."""
    if not company["eligible"]:
        raise BadRequest(company["reason"])
    if company["assignmentErrors"]:
        raise BadRequest(company["assignmentErrors"][0])
    activities = {a["id"]: a for a in as_list(template.get("activities")) if isinstance(a, dict)}
    new = [e for e in company["entries"] if e["status"] == "new"]
    created, already, unconfirmed, published = 0, 0, 0, 0
    now = now_iso()
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        for entry in new:
            activity = activities[entry["activity"]]
            if repo.insert_or_ignore(entry["id"], "task", _task_record(ctx, template, plan, company, entry, activity, batch_id, now)):
                created += 1
                unconfirmed += 0 if entry["confirmed"] else 1
                if entry["clientVisible"] and repo.insert_or_ignore(f"portal-{entry['id']}", "portalobligation",
                                                                    _portal_record(template, company, entry, activity)):
                    published += 1
            else:
                already += 1  # created concurrently since the plan was read: skipped, never duplicated
        counts = company["counts"]
        skipped = counts["existing"] + counts["duplicates"] + already
        AuditRepository.of(session, ctx).add(
            audit_label(ctx, f"Bulk calendar setup: created {created} entr{'y' if created == 1 else 'ies'}, skipped {skipped}"),
            f"{company['name']} · {template.get('name')} v{plan['template']['version']} · FY {plan['financialYear']}")
    return {"company": company["id"], "name": company["name"], "status": "Completed", "created": created,
            "existing": counts["existing"] + already, "duplicates": counts["duplicates"], "ruleCovered": counts["ruleCovered"],
            "excluded": counts["excluded"], "past": counts["past"], "needsConfirmation": unconfirmed, "published": published, "error": ""}


def _failed(company: dict, message: str) -> dict:
    return {"company": company["id"], "name": company["name"], "status": "Failed", "created": 0, "existing": 0, "duplicates": 0,
            "ruleCovered": 0, "excluded": 0, "past": 0, "needsConfirmation": 0, "published": 0, "error": message}


def _batch_status(selection: list[str], results: dict) -> str:
    states = [(results.get(c) or {}).get("status") for c in selection]
    if any(s is None for s in states):
        return "In progress"
    if all(s == "Completed" for s in states):
        return "Completed"
    return "Partially failed" if any(s == "Completed" for s in states) else "Failed"


def _start_batch(services: Services, ctx: Context, batch_id: str, template: dict, plan: dict, selection: list[str], names: dict) -> None:
    """The run's log record, written before any entry: who started it, when, for which companies."""
    now = now_iso()
    data = {
        "title": f"Bulk calendar setup · {template.get('name')} · FY {plan['financialYear']}",
        "template": {"id": template["id"], "name": template.get("name"), "version": plan["template"]["version"]},
        "financialYear": plan["financialYear"], "selection": selection, "companyNames": names,
        "activities": [a["id"] for a in plan["activities"] if a["included"]],
        "initiatedBy": ctx.actor["id"], "initiatedByName": ctx.actor.get("name"), "initiatedAt": now,
        "status": "In progress", "results": {}, "updatedAt": now,
    }
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        if repo.insert_or_ignore(batch_id, BATCH_KIND, data):
            listed = ", ".join(names.get(c) or c for c in selection[:25]) + (f" and {len(selection) - 25} more" if len(selection) > 25 else "")
            AuditRepository.of(session, ctx).add(
                audit_label(ctx, "Started bulk calendar setup"),
                f"{template.get('name')} v{plan['template']['version']} · FY {plan['financialYear']} · {len(selection)} "
                f"compan{'y' if len(selection) == 1 else 'ies'}: {listed}")
            return
        current = repo.get(batch_id)
    if current is None or current.get("kind") != BATCH_KIND or (current.get("template") or {}).get("id") != template["id"] \
            or current.get("financialYear") != plan["financialYear"]:
        raise Conflict("This bulk run belongs to another template or financial year. Start a new run.")


def _finish_batch(services: Services, ctx: Context, batch_id: str, results: list[dict]) -> dict:
    now = now_iso()
    for _ in range(5):
        with services.db.write() as session:
            repo = RecordRepository(session, ctx.owner)
            current = repo.get(batch_id)
            if current is None:
                raise NotFound("Bulk run not found.")
            merged = dict(current.get("results") or {})
            for result in results:
                previous = merged.get(result["company"]) or {}
                merged[result["company"]] = {**result, "createdTotal": (previous.get("createdTotal") or 0) + result["created"],
                                             "attempts": (previous.get("attempts") or 0) + 1, "by": ctx.actor["id"], "at": now}
            selection = [c for c in as_list(current.get("selection")) if isinstance(c, str)]
            names = {**(current.get("companyNames") or {}), **{r["company"]: r["name"] for r in results}}
            data = {**current, "results": merged, "companyNames": names, "status": _batch_status(selection, merged), "updatedAt": now}
            if repo.update_if_revision(batch_id, current["revision"], data):
                return data
    raise Conflict("The bulk run log changed while it was being updated. Refresh to see the latest results.")


def calendar_apply(services: Services, ctx: Context, body: dict) -> dict:
    """Creates the previewed entries for ``companies`` (one transaction per company).

    ``selection`` is every company chosen for the run (the UI sends the companies in turn, to show
    progress); ``run`` identifies the run, so all its requests and any retry share one log.
    """
    template = _load_template(ctx, body)
    if body.get("templateVersion") != (template.get("templateVersion") or 1):
        raise Conflict("This template changed after your preview. Review the preview again before creating calendars.")
    company_ids = _ids(body.get("companies"))
    if not company_ids:
        raise BadRequest("Select at least one company.")
    selection = unique([*_ids(body.get("selection")), *company_ids])
    if len(selection) > MAX_COMPANIES:
        raise BadRequest(f"Select at most {MAX_COMPANIES} companies at a time.")
    run = body.get("run") if isinstance(body.get("run"), str) and RUN_ID.fullmatch(body["run"]) else str(uuid.uuid4())
    batch_id = f"calbatch-{run}"
    plan = build_plan(ctx, template, body, company_ids)
    chosen = ctx.find_many(selection, "company")
    if any(c not in chosen or not can_access(ctx.actor, chosen[c]) for c in selection):
        raise NotFound("Company not found.")
    _start_batch(services, ctx, batch_id, template, plan, selection, {c: chosen[c].get("name") for c in selection})
    results = []
    for company in plan["companies"]:
        try:
            result = _apply_company(services, ctx, template, plan, company, batch_id)
        except ApiError as error:
            result = _failed(company, error.message)
        except Exception:  # noqa: BLE001 - one company's failure must not stop or hide the others
            log.exception("Bulk calendar setup failed for one company", extra={"event": "calendar_apply_error"})
            result = _failed(company, "The entries for this company could not be saved. Nothing was created for it; retry.")
        if result["status"] == "Failed":
            try:
                _audit(services, ctx, "Bulk calendar setup failed", f"{company['name']} · {template.get('name')} · {result['error']}")
            except Exception:  # noqa: BLE001 - the failure is still returned and logged in the run
                log.exception("Could not audit a bulk calendar failure", extra={"event": "calendar_audit_error"})
        results.append(result)
    batch = _finish_batch(services, ctx, batch_id, results)
    return {"ok": True, "run": run, "batch": batch_id, "status": batch["status"], "results": results}


# Calendar entries ---------------------------------------------------------------------------

def confirm_due(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    """Records that a person verified a template entry's due date for its company."""
    authorize(ctx, EDIT_ROLES)
    origin = r.get("template")
    if r.get("kind") != "task" or not isinstance(origin, dict):
        raise BadRequest("Only calendar entries created from a template have a date to confirm.")
    if r.get("status") == CANCELLED:
        raise BadRequest("This work was cancelled.")
    if origin.get("dateConfirmed") is True and origin.get("confirmedDue") in (None, r.get("due")):
        raise BadRequest("This due date is already confirmed.")
    now = now_iso()
    updated = {**r, "template": {**origin, "dateConfirmed": True, "confirmedBy": ctx.actor["id"], "confirmedAt": now, "confirmedDue": r.get("due")}}
    with services.db.write() as session:
        repo = RecordRepository(session, ctx.owner)
        if not repo.update_if_revision(r["id"], r["revision"], updated):
            raise Conflict(CHANGED)
        # The client calendar's provisional note goes with it (the date itself is mirrored live).
        mirror = repo.get(f"portal-{r['id']}")
        if mirror is not None and mirror.get("kind") == "portalobligation" and mirror.get("sourceTask") == r["id"] \
                and mirror.get("note") == PROVISIONAL_NOTE:
            repo.update_if_revision(mirror["id"], mirror["revision"], {**mirror, "note": ""})
        AuditRepository.of(session, ctx).add(audit_label(ctx, f"Confirmed due date {r.get('due')}"), r.get("title"))
    return {"ok": True, "id": r["id"]}


def company_status(services: Services, ctx: Context, body: dict, r: dict) -> dict:
    """Owner: marks a company active or inactive. An inactive company keeps every record and stays
    visible; it is only no longer eligible for bulk calendar setup."""
    authorize(ctx, ["Owner"])
    if r.get("kind") != "company":
        raise BadRequest("Select a company.")
    inactive = body.get("inactive") is True
    if (r.get("inactive") is True) == inactive:
        raise BadRequest("The company already has this status.")
    reason = text(body.get("reason"), 1000)
    if inactive and not reason:
        raise BadRequest("Record why the company is inactive.")
    data = {**r, "inactive": inactive, "inactiveReason": reason if inactive else "", "statusChangedBy": ctx.actor["id"],
            "statusChangedAt": now_iso()}
    return save(services, ctx, r, data, "Marked company inactive" if inactive else "Marked company active", subject=r.get("name"))
