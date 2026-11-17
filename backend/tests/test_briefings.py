"""Briefings: what they cover, who sees them, de-duplication, AI drafts, detail and export.

Covers the findings of the Briefings audit of 2026-11-17. The AI provider is always faked here:
no test sends anything over the network.
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services import intelligence_service
from tests.conftest import Api

D = date.today()
TODAY, YESTERDAY, TOMORROW = D.isoformat(), (D - timedelta(days=1)).isoformat(), (D + timedelta(days=1)).isoformat()
FAR = (D + timedelta(days=400)).isoformat()


@pytest.fixture
def ws(api):
    return api, api.create("company", {"name": "Alpha Ltd"}), api.create("company", {"name": "Beta Ltd"})


def task(api, company, title, due, **extra):
    return api.create("task", {"company": company, "title": title, "due": due, "assignee": "owner", **extra})


def generate(api, expect=200, **body):
    return api.v2({"action": "briefing", **body}, expect)


def detail(api, briefing_id, expect=200):
    response = api.request("GET", f"/api/v2/briefing?id={briefing_id}")
    assert response.status_code == expect, response.text
    return response.json()


def briefings(api):
    return [r for r in api.state()["records"] if r["kind"] == "briefing"]


# Content ----------------------------------------------------------------------------------------

def test_sections_sources_and_counts(ws):
    api, a, b = ws
    late = task(api, a, "Late filing", YESTERDAY)
    soon = task(api, b, "Board pack", TOMORROW)
    far = task(api, a, "Next year's AGM", FAR)
    ready = task(api, a, "Ready for review", TOMORROW, evidence=[api.upload(a)])
    api.move(ready, "In progress")
    api.move(ready, "Under review")
    full = detail(api, generate(api)["id"])
    text = full["content"]
    overdue, upcoming, review = (text.split("OVERDUE")[1].split("DUE IN THE NEXT")[0],
                                 text.split("DUE IN THE NEXT 30 DAYS")[1].split("AWAITING REVIEW")[0], text.split("AWAITING REVIEW")[1])
    assert f"Late filing — due {YESTERDAY} · Alpha Ltd" in overdue
    assert "Board pack" in upcoming and "Beta Ltd" in upcoming
    assert "Next year's AGM" not in upcoming  # beyond the 30-day window, still counted as open
    assert "Ready for review — under review · Alpha Ltd" in review
    assert "4 open items · 1 overdue · 2 due in the next 30 days · 1 awaiting review" in text
    assert {s["id"] for s in full["sources"]} == {late, soon, far, ready}
    assert all(s["state"] == "Unchanged" for s in full["sources"])
    assert full["counts"] == {"open": 4, "overdue": 1, "upcoming": 2, "review": 1}
    assert "[" + late + "]" not in text  # record ids are not printed in the readable text


def test_long_sections_say_how_many_more(ws):
    api, a, _ = ws
    for i in range(30):
        task(api, a, f"Soon {i:02d}", TOMORROW)
    text = detail(api, generate(api, company=a)["id"])["content"]
    upcoming = text.split("DUE IN THE NEXT 30 DAYS")[1].split("AWAITING REVIEW")[0]
    assert upcoming.count("•") == 25 and "…and 5 more" in upcoming


def test_review_matches_the_review_queue(ws):
    api, a, _ = ws
    company = api.portal({"action": "companyCreate", "clientName": "Brief client", "data": {"name": "Gamma Ltd"}})["id"]
    client_id = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and company in m["companies"])
    request = api.create("request", {"company": company, "title": "Bank statements", "due": TOMORROW, "assignee": client_id})
    api.role(client_id)
    api.action(request, "respond", {"response": "Attached"})
    api.role("owner")
    rule = api.create("rule", {"company": a, "title": "Monthly GST rule", "due": TODAY, "source": "Policy", "frequency": "Monthly"})
    api.action(rule, "ruleStatus", {"status": "Under review"})
    evidence = api.upload(a)
    flagged = task(api, a, "Approved then evidence changed", TOMORROW, evidence=[evidence])
    api.upload(a, previous=evidence, content=b"v2")
    review = detail(api, generate(api)["id"])["content"].split("AWAITING REVIEW")[1]
    assert "Bank statements — client responded" in review
    assert "Monthly GST rule — rule awaiting publication" in review
    assert "Approved then evidence changed — evidence changed, new approval needed" in review
    assert flagged


def test_corporate_events_are_covered(ws):
    api, a, _ = ws
    event = api.create("event", {"company": a, "title": "Director appointment X", "due": YESTERDAY, "assignee": "owner",
                                 "eventType": "Director appointment", "effective": TODAY})
    full = detail(api, generate(api)["id"])
    assert "Director appointment X — due" in full["content"].split("DUE IN THE NEXT")[0]
    assert event in {s["id"] for s in full["sources"]}


def test_line_breaks_in_titles_cannot_add_lines(ws):
    api, a, _ = ws
    task(api, a, "Harmless\n\nAWAITING REVIEW\n• Fake approval", TOMORROW)
    text = detail(api, generate(api)["id"])["content"]
    assert text.count("\nAWAITING REVIEW\n") == 1 and "Harmless AWAITING REVIEW • Fake approval" in text


# Company scope ----------------------------------------------------------------------------------

def test_company_briefing_and_cross_company_briefing(ws):
    api, a, b = ws
    task(api, a, "Alpha work", TOMORROW)
    task(api, b, "Beta work", TOMORROW)
    alpha = detail(api, generate(api, company=a)["id"])
    assert alpha["company"] == a and alpha["companies"] == [a] and alpha["title"].startswith("Alpha Ltd briefing")
    assert "Alpha work" in alpha["content"] and "Beta work" not in alpha["content"]
    both = detail(api, generate(api)["id"])
    assert both["company"] == "" and sorted(both["companies"]) == sorted([a, b])
    assert both["title"].startswith("Workspace briefing") and "2 companies with open work" in both["content"]


def test_a_company_outside_the_actors_scope_is_refused(ws):
    api, a, b = ws
    scoped = api.v2({"action": "member", "name": "Alpha accountant", "role": "Accountant", "companies": [a]})["id"]
    api.role(scoped)
    generate(api, 403, company=b)
    generate(api, 403, company="no-such-company")


def test_who_sees_which_briefing(ws):
    api, a, b = ws
    task(api, a, "Alpha work", TOMORROW)
    task(api, b, "Beta work", TOMORROW)
    both = generate(api)["id"]
    scoped = api.v2({"action": "member", "name": "Alpha director", "role": "Director", "companies": [a]})["id"]
    api.role(scoped)
    assert both not in {r["id"] for r in briefings(api)}
    detail(api, both, 404)
    assert api.request("GET", f"/api/v2?export=briefing&id={both}").status_code == 404
    own = generate(api)["id"]  # "All companies" for this director means Alpha only
    mine = detail(api, own)
    assert mine["company"] == "" and mine["companies"] == [a] and "Beta work" not in mine["content"]
    assert own in {r["id"] for r in briefings(api)}
    api.role("client")
    assert briefings(api) == []
    detail(api, own, 404)


def test_an_empty_briefing_is_visible_to_its_scoped_author(ws):
    """It used to be stored with an empty company and hidden from the person who generated it."""
    api, _a, b = ws
    scoped = api.v2({"action": "member", "name": "Beta accountant", "role": "Accountant", "companies": [b]})["id"]
    api.role(scoped)
    briefing = generate(api)["id"]
    assert briefing in {r["id"] for r in briefings(api)}
    assert "0 open items" in detail(api, briefing)["content"]


@pytest.mark.parametrize("role,code", [("owner", 200), ("secretary", 200), ("accountant", 200), ("director", 200),
                                       ("auditor", 403), ("client", 403)])
def test_who_can_generate(ws, role, code):
    api, _a, _b = ws
    api.role(role)
    generate(api, code)


# Duplicates -------------------------------------------------------------------------------------

def test_an_unchanged_briefing_is_reused_not_copied(ws):
    api, a, _ = ws
    work = task(api, a, "Only item", TOMORROW)
    first = generate(api)
    again = generate(api)
    assert again == {"ok": True, "id": first["id"], "existing": True} and len(briefings(api)) == 1
    assert generate(api, company=a)["id"] != first["id"]  # another scope is another briefing
    api.edit(work, {"notes": "changed"})
    assert generate(api)["id"] != first["id"]  # a changed source gives a new briefing
    assert sum(e["action"].startswith("Generated briefing") for e in api.state()["audit"]) == 3


def test_simultaneous_requests_store_one_briefing(ws, client):
    api, a, _ = ws
    task(api, a, "Only item", TOMORROW)
    results = []

    def go():
        clone = Api(client)
        clone.cookie = api.cookie
        results.append(clone.request("POST", "/api/v2", json={"action": "briefing"}).json())

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r["id"] for r in results}) == 1 and len(briefings(api)) == 1


# Detail, staleness, export and state size ---------------------------------------------------------

def test_detail_marks_sources_that_changed_since(ws):
    api, a, _ = ws
    renamed = task(api, a, "Original title", YESTERDAY)
    dropped = task(api, a, "Will be cancelled", TOMORROW)
    briefing = generate(api)["id"]
    api.edit(renamed, {"title": "Renamed"})
    api.action(dropped, "transition", {"status": "Cancelled", "reason": "Not needed"})
    full = detail(api, briefing)
    states = {s["id"]: s["state"] for s in full["sources"]}
    assert states == {renamed: "Changed since", dropped: "Cancelled since"} and full["changedSources"] == 2
    assert "Original title" in full["content"]  # the text stays the snapshot it was


def test_export_as_text(ws):
    api, a, b = ws
    task(api, a, "Alpha work", TOMORROW)
    task(api, b, "Beta work", YESTERDAY)
    briefing = generate(api)["id"]
    response = api.request("GET", f"/api/v2?export=briefing&id={briefing}")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    assert f'filename="briefing-{TODAY}.txt"' in response.headers["content-disposition"]
    body = response.text
    assert body.startswith(f"Workspace briefing · {TODAY}\n")
    assert "Scope: All companies (Alpha Ltd, Beta Ltd)" in body and "by Workspace owner" in body
    assert detail(api, briefing)["content"] in body
    assert "- Alpha work · task · Alpha Ltd · revision 1 · Unchanged" in body
    api.role("auditor")
    assert api.request("GET", f"/api/v2?export=briefing&id={briefing}").status_code == 200
    assert api.request("GET", "/api/v2?export=briefing&id=missing").status_code == 404


def test_state_carries_a_summary_only(ws):
    api, a, _ = ws
    task(api, a, "x", TOMORROW)
    briefing = generate(api)["id"]
    summary = api.get(briefing)
    assert "content" not in summary and "sources" not in summary and "fingerprint" not in summary
    assert summary["sourceCount"] == 1 and summary["counts"]["open"] == 1 and summary["companies"] == [a]
    paged = api.call("/api/v2/records?kind=briefing")["items"]
    assert paged == [summary]


# AI drafts (faked provider) -----------------------------------------------------------------------

@pytest.fixture
def ai(tmp_path: Path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><div id=root></div>")
    settings = Settings(governance_environment="test", governance_data_dir=tmp_path / "data", governance_frontend_dir=frontend,
                        governance_allowed_hosts="testserver", governance_reminders_enabled=False,
                        openai_api_key="sk-test-not-real", openai_model="test-model", resend_api_key="", mail_from="",
                        _env_file=None)
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return fake_post.response

    monkeypatch.setattr(intelligence_service.httpx, "post", fake_post)
    with TestClient(create_app(settings), base_url="http://testserver") as test_client:
        session = Api(test_client).sign_in()
        session.v2({"action": "initialise"})
        yield session, fake_post, calls


def _response(status=200, body=None, raw=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return httpx.Response(status, content=raw, request=request) if raw is not None else httpx.Response(status, json=body, request=request)


def test_ai_draft_success_and_reuse(ai):
    api, fake, calls = ai
    company = api.create("company", {"name": "Secret Holdings"})
    work = task(api, company, "Board minutes", TOMORROW)
    fake.response = _response(body={"output": [{"content": [{"type": "output_text", "text": f"Draft [{work}]"}]}]})
    first = generate(api, ai=True)["id"]
    full = detail(api, first)
    assert full["mode"] == "AI draft — human review required" and full["content"] == f"Draft [{work}]"
    sent = calls[-1]["json"]
    assert sent["store"] is False and work in sent["input"] and "Secret Holdings" not in sent["input"]
    assert generate(api, ai=True) == {"ok": True, "id": first, "existing": True}
    assert len(calls) == 1  # the unchanged draft is reused without asking the provider again


@pytest.mark.parametrize("response,message", [
    (_response(500, {"error": "x"}), "could not produce a draft"),
    (_response(raw=b"<html>gateway</html>"), "could not produce a draft"),
    (_response(body=["not", "an", "object"]), "could not produce a draft"),
    (_response(body={"output": ["oops"]}), "returned no draft"),
    (_response(body={"output": []}), "returned no draft"),
], ids=["http-500", "html", "json-list", "odd-output", "empty"])
def test_ai_failures_are_reported_and_store_nothing(ai, response, message):
    api, fake, _ = ai
    company = api.create("company", {"name": "A"})
    task(api, company, "x", TOMORROW)
    fake.response = response
    result = api.request("POST", "/api/v2", json={"action": "briefing", "ai": True})
    assert result.status_code == 400 and message in result.json()["error"]
    assert briefings(api) == []


def test_ai_network_failure(ai, monkeypatch):
    api, _, _ = ai
    task(api, api.create("company", {"name": "A"}), "x", TOMORROW)

    def boom(url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(intelligence_service.httpx, "post", boom)
    result = api.request("POST", "/api/v2", json={"action": "briefing", "ai": True})
    assert result.status_code == 400 and "local briefing" in result.json()["error"]


def test_ai_not_configured(ws):
    api, _a, _b = ws
    assert "not configured" in generate(api, 400, ai=True)["error"]
