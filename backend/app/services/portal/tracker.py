"""Client-facing work tracker: a projection of the portal state the client may already see.

Pure functions only. ``portal_state()`` has already applied organization scoping, company scoping,
the "shared documents only" rule and the request-assignee rule; this module decides which of that
data reads as a *work item* and in which client-facing words. It performs no queries, no writes and
opens no new access path, so it cannot widen what a client can see.

Two rules shape every mapping below:

* **No invention.** A field is included only when the record carries it. Timestamps come from the
  records themselves, never from the current status. The only progress figure is the onboarding
  milestone count, which is real persisted state (six verified-in-order stages).
* **Conservative status wording.** "Awaiting Your Action" is used only where the stored data says
  the client is the one who must act (``Waiting for client``, or a request assigned to the client).
  An internally approved item is *not* reported as completed, and an unmapped status is reported as
  no status at all rather than guessed at.
"""
from __future__ import annotations

from typing import Any

from app.core.jsvalues import as_list, date_valid, lt, text, truthy

# Client-facing statuses (display vocabulary; the records keep their own statuses).
NOT_STARTED = "Not Started"
IN_PROGRESS = "In Progress"
AWAITING_CLIENT = "Awaiting Your Action"
UNDER_REVIEW = "Under Review"
CHANGES_REQUESTED = "Changes Requested"
COMPLETED = "Completed"
CANCELLED = "Cancelled"
NOT_APPLICABLE = "Not Applicable"
NEEDS_SCHEDULING = "Needs Scheduling"

# A work item in one of these is finished as far as the client is concerned: never overdue.
SETTLED = (COMPLETED, CANCELLED, NOT_APPLICABLE)
# Only these two say the client owes something, and only explicit source data produces them.
CLIENT_ACTION = (AWAITING_CLIENT, CHANGES_REQUESTED)

# Compliance items. The first group is the obligation's own vocabulary (OBLIGATION_STATUSES plus
# the "Needs scheduling" value ``annual_plan`` writes); the second is the internal task lifecycle,
# which ``portal_state`` substitutes for a published obligation's status.
OBLIGATION_STATUS = {
    "Not started": NOT_STARTED,
    "In progress": IN_PROGRESS,
    "Waiting for client": AWAITING_CLIENT,
    "Completed": COMPLETED,
    "Not applicable": NOT_APPLICABLE,
    "Needs scheduling": NEEDS_SCHEDULING,
    # Mirrored internal lifecycle. "Awaiting information" does not record *who* owes the
    # information, so it stays neutral; "Approved" is an internal approval, not completion.
    "Awaiting information": IN_PROGRESS,
    "Under review": UNDER_REVIEW,
    "Approved": UNDER_REVIEW,
    "Cancelled": CANCELLED,
    "Review required": UNDER_REVIEW,
}
# Onboarding stages. "Review required" is computed by ``portal_state`` when a verified stage's
# evidence went stale, which means the secretary must look again — not that the client must act.
STAGE_STATUS = {
    "Not started": NOT_STARTED,
    "In progress": IN_PROGRESS,
    "Waiting for client": AWAITING_CLIENT,
    "Verified": COMPLETED,
    "Review required": UNDER_REVIEW,
}
# Information requests are assigned to a client, so "Open" and "Returned" do name the client.
REQUEST_STATUS = {
    "Open": AWAITING_CLIENT,
    "Returned": CHANGES_REQUESTED,
    "Responded": UNDER_REVIEW,
    "Closed": COMPLETED,
}
# The stored statuses in which the client may still upload documents and respond (the statuses
# ``governance_service.respond`` and ``documents_service.upload_document`` accept).
REQUEST_ANSWERABLE = ("Open", "Returned")
REQUEST_PRIORITIES = ("Normal", "High", "Urgent")

# Plain-English explanation of each client-facing status.
STATUS_NOTES = {
    NOT_STARTED: "Your secretary has scheduled this work but has not started it yet.",
    IN_PROGRESS: "Your secretary is working on this.",
    AWAITING_CLIENT: "Your secretary needs something from you before this can move on.",
    UNDER_REVIEW: "The work is being checked before it is finalised.",
    CHANGES_REQUESTED: "Your secretary has asked for a correction or more detail.",
    COMPLETED: "This work is finished.",
    CANCELLED: "This work was cancelled and is kept for your records.",
    NOT_APPLICABLE: "Your secretary reviewed this and it does not apply to your company.",
    NEEDS_SCHEDULING: "Your secretary is confirming the applicable due date.",
}
NO_ACTION = "No further action is currently required from you."
NEXT_ACTIONS = {
    AWAITING_CLIENT: "Send your secretary the information or documents requested.",
    CHANGES_REQUESTED: "Review the note below and supply the corrected information.",
    UNDER_REVIEW: NO_ACTION,
    IN_PROGRESS: NO_ACTION,
    NOT_STARTED: NO_ACTION,
    NEEDS_SCHEDULING: NO_ACTION,
    COMPLETED: NO_ACTION,
    CANCELLED: NO_ACTION,
    NOT_APPLICABLE: NO_ACTION,
}
REQUEST_NEXT_ACTION = "Open the request under Requests, upload the document asked for and submit it for review."
# Timeline labels. Each one is only emitted when the underlying record holds that timestamp:
# nothing here is derived from the current status.
EVENT_LABELS = {"created": "Work opened", "respondedAt": "You responded",
                "completedAt": "Completed", "cancelledAt": "Cancelled"}

ONBOARDING_CATEGORY = "Onboarding"
REQUEST_CATEGORY = "Information request"


def _documents(ids: Any, shared: dict[str, dict]) -> list[dict]:
    """The subset of ``ids`` that are documents published to this client, as {id, name}.

    Attachments are validated as published when they are saved, but publication can be withdrawn
    afterwards, so the current state decides. Nothing else about the document is exposed.
    """
    out, seen = [], set()
    for document_id in as_list(ids):
        document = shared.get(document_id) if isinstance(document_id, str) else None
        if document is not None and document_id not in seen:
            seen.add(document_id)
            out.append({"id": document["id"], "name": text(document.get("name"), 300)})
    return out


def _events(facts: dict, keys: tuple[str, ...]) -> list[dict]:
    """Persisted events only, oldest first. A key with no stored timestamp produces no event."""
    events = [{"key": key, "label": EVENT_LABELS[key], "at": text(facts.get(key), 60)}
              for key in keys if text(facts.get(key), 60)]
    return sorted(events, key=lambda e: e["at"])


def _overdue(due: Any, status: str | None, today: str) -> bool:
    """A real due date, earlier than the server's today, on work that is not settled.

    Due *today* is not overdue, and the browser's clock is never consulted: ``today`` comes from
    ``portal_state``.
    """
    return bool(date_valid(due) and date_valid(today) and lt(due, today) and status not in SETTLED)


def _item(source: str, item_id: str, title: str, company: Any, category: str, status: str | None,
          *, period: Any = None, updated: Any = None, due: Any = None, note: Any = None,
          documents: list[dict] | None = None, today: str = "", completed: Any = None,
          cancelled: Any = None, timeline: list[dict] | None = None, extra: dict | None = None) -> dict:
    """One tracker row. Every field is named here: internal records are never spread into it.

    ``completed``/``cancelled`` carry a timestamp only where the workflow writes one (an internal
    record's ``completedAt``/``cancelledAt``); the row's ``updated`` is never reused as a
    completion date, because a record is updated for many reasons.
    """
    client_action = status in CLIENT_ACTION
    next_action = REQUEST_NEXT_ACTION if (source == "request" and client_action) else NEXT_ACTIONS.get(status)
    return {
        "id": item_id,
        "source": source,
        "title": title,
        "company": company if isinstance(company, str) else None,
        "category": category,
        "period": text(period, 120) or None,
        "status": status,
        "statusNote": STATUS_NOTES.get(status) or "Your secretary has not reported a status for this item yet.",
        "updated": text(updated, 60) or None,
        "due": text(due, 60) if date_valid(due) else None,
        "overdue": _overdue(due, status, today),
        "clientAction": client_action,
        "nextAction": next_action,
        "documents": documents or [],
        "note": text(note, 2000) or None,
        "completed": text(completed, 60) or None,
        "cancelled": text(cancelled, 60) or None,
        "timeline": timeline or [],
        **(extra or {}),
    }


def _obligation_item(record: dict, shared: dict[str, dict], today: str, facts: dict) -> dict:
    """A compliance item, including one published from internal work.

    ``portal_state`` has already replaced a published item's status and due date with the linked
    record's, and hands this projection that record's client-safe timestamps in ``facts``. The link
    itself (``sourceTask``) stays internal and is not carried over, and an item a secretary
    maintains by hand carries no timestamps at all — none are invented for it.
    """
    status = OBLIGATION_STATUS.get(text(record.get("status")))
    return _item(
        "obligation", record["id"], text(record.get("title"), 300), record.get("company"),
        text(record.get("category"), 120) or "Compliance", status,
        period=record.get("period"), updated=record.get("updated"), due=record.get("due"),
        note=record.get("note"), documents=_documents(record.get("evidence"), shared), today=today,
        completed=facts.get("completedAt") if status == COMPLETED else None,
        cancelled=facts.get("cancelledAt") if status == CANCELLED else None,
        timeline=_events(facts, ("created", "completedAt", "cancelledAt")),
    )


def _stage_status(stage: dict) -> str | None:
    return STAGE_STATUS.get(text(stage.get("status")))


def _onboarding_item(record: dict, shared: dict[str, dict], today: str) -> dict:
    """The company's onboarding as one work item, with its six persisted stages as milestones.

    The item's status is derived from the stages that are stored, not invented: a stage waiting on
    the client wins, then every stage verified, then a stage under review, then work in progress.
    """
    stages, milestones, documents = as_list(record.get("stages")), [], []
    for stage in stages:
        status = _stage_status(stage)
        files = _documents(stage.get("evidence"), shared)
        documents.extend(files)
        milestones.append({
            "id": text(stage.get("id"), 120), "title": text(stage.get("title"), 300),
            "description": text(stage.get("description"), 500) or None, "status": status,
            "statusNote": STATUS_NOTES.get(status),
            "due": text(stage.get("due"), 60) if date_valid(stage.get("due")) else None,
            "updated": text(stage.get("updated"), 60) or None,
            "note": text(stage.get("note"), 2000) or None, "documents": files,
        })
    statuses = [m["status"] for m in milestones]
    verified = statuses.count(COMPLETED)
    if AWAITING_CLIENT in statuses:
        status = AWAITING_CLIENT
    elif statuses and verified == len(statuses):
        status = COMPLETED
    elif UNDER_REVIEW in statuses:
        status = UNDER_REVIEW
    elif IN_PROGRESS in statuses or verified:
        status = IN_PROGRESS
    else:
        status = NOT_STARTED
    # The active milestone supplies the next due date and the client-visible progress note.
    active = next((m for m in milestones if m["status"] != COMPLETED), None)
    due = next((m["due"] for m in milestones if m["status"] != COMPLETED and m["due"]), None)
    updated = max([m["updated"] for m in milestones if m["updated"]], default=None) or record.get("updated")
    unique = {d["id"]: d for d in documents}
    return _item(
        "onboarding", record["id"], text(record.get("title"), 300) or "Company onboarding",
        record.get("company"), ONBOARDING_CATEGORY, status,
        updated=updated, due=due, note=(active or {}).get("note"),
        documents=list(unique.values()), today=today,
        extra={"milestones": milestones, "progress": {"verified": verified, "total": len(milestones)}},
    )


def _request_item(record: dict, shared: dict[str, dict], today: str) -> dict:
    """An information request. ``reviewNote`` is the correction the secretary asked the client for
    (``request_decision`` requires it when a response is returned), so it is client-facing.

    The same row feeds the portal's Requests page, so ``request`` adds what answering it takes: the
    stored status (the page's filters key on it), the revision the response is sent against, the
    secretary's description and priority, the client's own last response and whether it still
    accepts an answer. Staff discussion, reviewers and unpublished documents are never included:
    ``documents`` are the request's uploads that are currently published to the client.
    """
    stored = text(record.get("status"))
    status = REQUEST_STATUS.get(stored)
    note = record.get("reviewNote") if status == CHANGES_REQUESTED else None
    revision = record.get("revision")
    priority = record.get("priority")
    # Closing a request records who decided it but no timestamp, so there is no completion date.
    return _item(
        "request", record["id"], text(record.get("title"), 300), record.get("company"),
        REQUEST_CATEGORY, status, updated=record.get("updated"), due=record.get("due"),
        note=note, documents=_documents(record.get("evidence"), shared), today=today,
        timeline=_events(record, ("created", "respondedAt")),
        extra={"request": {
            "status": stored if stored in REQUEST_STATUS else None,
            "revision": revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
            "description": text(record.get("notes"), 12000) or None,
            "priority": priority if priority in REQUEST_PRIORITIES else None,
            "response": text(record.get("response"), 10000) or None,
            # A resubmitted request keeps the correction it answers; a closed one has none.
            "correction": (text(record.get("reviewNote"), 2000) or None) if stored in ("Returned", "Responded") else None,
            "answerable": stored in REQUEST_ANSWERABLE,
        }},
    )


def _order(items: list[dict]) -> list[dict]:
    """What needs the client first, then what is late, then by due date, then most recently updated."""
    return sorted(items, key=lambda i: (
        not i["clientAction"], not i["overdue"], i["due"] or "9999-12-31", _invert(i["updated"]), i["title"]))


def _invert(value: str | None) -> str:
    """Sort key that puts the most recent timestamp first within an equal group."""
    return "".join(chr(0x10FFFD - ord(c)) if ord(c) < 0x10FFFD else c for c in (value or ""))


def work_items(state: dict, today: str | None = None, mirrored: dict[str, dict] | None = None) -> list[dict]:
    """Client-facing work items derived from one ``portal_state()`` payload.

    Sources: compliance obligations (including work published from an internal task), company
    onboarding, and information requests. Internal records never reach this projection — the state
    it reads has already been filtered to what the client may see, and ``internalTasks`` (staff
    only) is deliberately not a source, which is also why a published task cannot appear twice.

    ``mirrored`` maps a published item's id to the client-safe timestamps of the internal record
    behind it (``created``, ``completedAt``, ``cancelledAt``). ``portal_state`` builds it while it
    mirrors the status, and it never becomes part of the portal payload.

    Invoices and payments are not included in this phase; see the module's documentation.
    """
    facts = mirrored or {}
    day = text(today if today is not None else state.get("today"), 60)
    shared = {d["id"]: d for d in as_list(state.get("documents"))
              if isinstance(d, dict) and isinstance(d.get("id"), str) and truthy(d.get("shared"))}
    items: list[dict] = []
    for record in as_list(state.get("obligations")):
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            items.append(_obligation_item(record, shared, day, facts.get(record["id"]) or {}))
    for record in as_list(state.get("cases")):
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            items.append(_onboarding_item(record, shared, day))
    for record in as_list(state.get("requests")):
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            items.append(_request_item(record, shared, day))
    unique: dict[tuple[str, str], dict] = {}
    for item in items:  # one row per underlying record, whichever list it arrived in
        unique.setdefault((item["source"], item["id"]), item)
    return _order(list(unique.values()))
