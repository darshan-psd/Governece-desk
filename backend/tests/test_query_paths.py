"""The optimised query paths must return exactly what the original whole-workspace logic returned."""
import json
import sqlite3
from datetime import date

import pytest

from app.auth import Principal
from app.permissions import accessible, client_safe
from app.repositories.records import RecordRepository
from app.services.context import load_context
from app.services.portal.state import portal_state
from app.services.records_service import read_state, read_state_json

WS = "local-preview"


@pytest.fixture
def busy(api, client):
    """A workspace with every record kind, multi-company scope, versions, briefings and billing."""
    today = date.today().isoformat()
    c1 = api.create("company", {"name": "One"})
    c2 = api.create("company", {"name": "Two"})
    doc = api.upload(c1, content=b"v1")
    api.upload(c1, previous=doc, content=b"v2")
    shared = api.upload(c2, content=b"shared")
    api.action(shared, "documentShare", {"shared": True})
    for i in range(3):
        api.create("task", {"company": c1, "title": f"Task {i}", "due": today, "assignee": "owner"})
    api.create("meeting", {"company": c2, "title": "Board", "due": today, "agenda": "A"})
    api.create("event", {"company": c1, "title": "Appoint", "due": today, "eventType": "Director appointment", "effective": today})
    api.create("rule", {"company": c1, "title": "Rule", "due": today, "source": "Policy", "frequency": "Monthly"})
    scoped = api.v2({"action": "member", "name": "Scoped staff", "role": "Accountant", "companies": [c2]})["id"]
    client_id = api.v2({"action": "member", "name": "Client", "role": "Client", "companies": [c2]})["id"]
    api.create("request", {"company": c2, "title": "Docs please", "due": today, "assignee": client_id})
    api.v2({"action": "automation"})
    api.v2({"action": "briefing"})
    inv = api.portal({"action": "invoiceCreate", "company": c2, "data": {"number": "N1", "due": today, "items": [{"description": "Fee", "quantity": 1, "rate": "100"}],
                                                                       "seller": {"name": "Firm", "address": "Street"}}})["id"]
    api.portal({"action": "companySave", "id": c2, "revision": api.get(c2)["revision"], "data": {"name": "Two", "address": "Road"}})
    services = client.app.state.services
    # Edge cases the original code tolerated: non-object JSON data, a briefing with a missing source,
    # unicode text and escaped characters.
    db = sqlite3.connect(services.db.path)
    db.execute("INSERT INTO records VALUES (?,?,?,?,1,?)", (WS, "odd", "task", "[1,2]", "2026-01-01T00:00:00.000Z"))
    db.execute("INSERT INTO records VALUES (?,?,?,?,1,?)", (WS, "orphan-briefing", "briefing",
               json.dumps({"title": "Orphan", "company": c1, "sources": [{"id": "gone"}]}), "2026-01-01T00:00:00.000Z"))
    db.execute("INSERT INTO records VALUES (?,?,?,?,1,?)", (WS, "uni", "task",
               json.dumps({"title": "Résumé — \"quoted\" \\ back", "company": c1, "key": "secret/path"}, ensure_ascii=True), "2026-01-01T00:00:00.000Z"))
    db.commit()
    db.close()
    return {"services": services, "c1": c1, "c2": c2, "scoped": scoped, "client": client_id, "invoice": inv}


def reference_state(services, ctx) -> list[dict]:
    """The original algorithm: load everything, filter in Python."""
    with services.db.read() as session:
        everything = RecordRepository(session, WS).all()
    return [client_safe(ctx, {k: v for k, v in r.items() if k != "key"}) for r in everything if accessible(ctx, r) and r.get("kind") != "member"]


@pytest.mark.parametrize("actor", ["owner", "secretary", "scoped", "client"])
def test_state_records_match_the_original_algorithm(busy, actor):
    services = busy["services"]
    actor_id = busy.get(actor, actor)
    ctx = load_context(services, Principal(WS, actor_id))
    expected = reference_state(services, load_context(services, Principal(WS, actor_id)))
    assert read_state(services, ctx)["records"] == expected
    fast = json.loads(read_state_json(services, load_context(services, Principal(WS, actor_id))))
    assert fast["records"] == json.loads(json.dumps(expected))
    assert list(fast) == ["records", "actor", "members", "audit", "connections", "local", "date", "eventTemplates"]
    if actor == "owner":
        ids = [r["id"] for r in fast["records"]]
        assert "orphan-briefing" not in ids and "odd" in ids and all("key" not in r for r in fast["records"])
        assert next(r for r in fast["records"] if r["id"] == "uni")["title"] == "Résumé — \"quoted\" \\ back"


def test_portal_company_slice_matches_full_state(busy):
    services = busy["services"]
    ctx = load_context(services, Principal(WS, "owner"))
    full = portal_state(ctx)
    one = portal_state(load_context(services, Principal(WS, "owner")), only_company=busy["c2"])
    for key in ("invoices", "payments", "documents", "obligations", "cases", "threads", "requests", "calendar"):
        assert one[key] == [r for r in full[key] if r.get("company") == busy["c2"]], key


# Paginated endpoints (new, additive) ------------------------------------------------------------

def all_pages(api, path: str, limit: int) -> tuple[list[dict], int]:
    items, offset = [], 0
    while True:
        page = api.call(f"{path}{'&' if '?' in path else '?'}limit={limit}&offset={offset}")
        items += page["items"]
        offset += limit
        if offset >= page["total"]:
            return items, page["total"]


@pytest.mark.parametrize("actor", ["owner", "scoped", "client"])
def test_paginated_records_equal_the_full_state(api, busy, actor):
    if actor != "owner":
        api.role(busy[actor])
    expected = api.state()["records"]
    items, total = all_pages(api, "/api/v2/records", limit=3)
    assert total == len(expected)
    assert items == expected  # same records, same order, same visibility rules


def test_record_filters_and_sorting_are_applied_in_sql(api, busy):
    everything = api.state()["records"]
    page = api.call(f"/api/v2/records?kind=task&company={busy['c1']}&limit=500")
    assert page["items"] == [r for r in everything if r["kind"] == "task" and r.get("company") == busy["c1"]]
    multi = api.call(f"/api/v2/records?kind=task,meeting&status=Not started&limit=500")["items"]
    assert multi and all(r["kind"] in ("task", "meeting") and r["status"] == "Not started" for r in multi)
    by_due = api.call("/api/v2/records?kind=task&sort=due&order=asc&limit=500")["items"]
    dues = [r.get("due") for r in by_due if isinstance(r.get("due"), str)]
    assert dues == sorted(dues)
    today = date.today().isoformat()
    dated = api.call(f"/api/v2/records?due_from={today}&due_to={today}&limit=500")["items"]
    assert dated and all(r["due"] == today for r in dated)
    assert api.call("/api/v2/records?assignee=owner&kind=task&limit=1")["total"] >= 3


def test_record_query_validation(api):
    for bad in ("limit=0", "limit=501", "offset=-1", "sort=data", "order=up", "due_from=2026-02-30", "unknown=1"):
        response = api.request("GET", f"/api/v2/records?{bad}")
        assert response.status_code == 400 and "Invalid query parameter" in response.json()["error"], bad


def test_client_paging_hides_internal_records(api, busy):
    api.role(busy["client"])
    page = api.call("/api/v2/records?limit=500")
    assert {r["kind"] for r in page["items"]} <= {"company", "document", "request"}
    assert all("comments" not in r for r in page["items"] if r["kind"] == "company")
    assert api.call(f"/api/v2/records?company={busy['c1']}")["total"] == 0  # another company
    assert api.request("GET", "/api/v2/audit").status_code == 403


def test_audit_trail_pages_through_every_entry(api, busy):
    first = api.call("/api/v2/audit?limit=5")
    assert first["total"] > 5 and len(first["items"]) == 5
    items, total = all_pages(api, "/api/v2/audit", limit=4)
    assert len(items) == total and len({i["id"] for i in items}) == total
    assert [i["at"] for i in items] == sorted((i["at"] for i in items), reverse=True)
    assert items[: len(api.state()["audit"])] == api.state()["audit"]  # state keeps its newest-300 contract
