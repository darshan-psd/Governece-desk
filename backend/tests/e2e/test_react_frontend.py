"""End-to-end check of the unchanged React frontend running against FastAPI.

Starts the API on a throwaway data folder, opens the built app in Chrome (Playwright, system
Chrome channel) and drives every sidebar module plus the main create/edit/workflow/permission
paths through the real UI.

Run with:  GOVERNANCE_E2E=1 .venv/bin/python -m pytest tests/e2e -s
Requires:  pnpm frontend:build, Google Chrome, pip install -r requirements-dev.txt
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("GOVERNANCE_E2E") != "1", reason="set GOVERNANCE_E2E=1 to run the browser test")

BACKEND = Path(__file__).resolve().parents[2]
SIDEBAR = ["Overview", "My work", "Companies", "Calendar", "Review queue", "Board room", "Corporate events", "Client portal",
           "Client requests", "Documents", "Accounting", "Rule library", "Submissions", "Reports", "Briefings", "Inbox",
           "Team & access", "Connections", "Audit trail"]
PORTAL_TABS = ["Overview", "Company profile", "Onboarding", "Documents", "Inbox", "Requests", "Compliance calendar", "Invoices & payments"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    env = {**os.environ, "GOVERNANCE_ENVIRONMENT": "test", "GOVERNANCE_DATA_DIR": str(tmp_path_factory.mktemp("e2e-data")),
           "GOVERNANCE_REMINDERS_ENABLED": "false"}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                               cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield base
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
def page(server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        page.api_failures, page.page_errors, page.csp_violations = [], [], []

        def on_response(r):
            if "/api/" in r.url and r.status >= 500:
                page.api_failures.append(f"{r.status} {r.request.method} {r.url}")
            if "/api/" in r.url and not r.headers.get("x-request-id"):
                page.api_failures.append(f"missing X-Request-ID: {r.request.method} {r.url}")

        page.on("response", on_response)
        page.on("pageerror", lambda e: page.page_errors.append(str(e)))
        # The Content-Security-Policy must not block anything the React app does.
        page.on("console", lambda m: page.csp_violations.append(m.text) if "Content Security Policy" in m.text else None)
        page.add_init_script("document.addEventListener('securitypolicyviolation', e => console.error("
                             "'Content Security Policy violation: ' + e.violatedDirective + ' ' + e.blockedURI))")
        page.goto(server + "/")
        page.get_by_text("Good work starts with a clear view.").wait_for()
        yield page
        browser.close()


def expect(locator):
    from playwright.sync_api import expect as playwright_expect

    return playwright_expect(locator)


def nav(page, name: str) -> None:
    # Sidebar items may carry a count badge ("Review queue 1").
    page.locator("aside").get_by_role("button", name=re.compile(rf"^{re.escape(name)}( \d+)?$")).first.click()
    page.wait_for_load_state("networkidle")


def no_error(page, where: str) -> None:
    banners = [t for t in page.locator(".error-banner").all_inner_texts() if t.strip()]
    assert not banners, f"{where}: {banners}"
    assert not page.api_failures, f"{where}: {page.api_failures}"
    assert not page.page_errors, f"{where}: {page.page_errors}"
    assert not page.csp_violations, f"{where}: {page.csp_violations}"


def toast(page, text: str) -> None:
    page.get_by_text(text).first.wait_for(timeout=5000)


def close_details(page) -> None:
    close = page.get_by_role("button", name="Close details")
    if close.count():
        expect(close).to_be_enabled()
        close.click()


def preview_as(page, label: str) -> None:
    # The internal workspace and the client portal each have their own local-preview switcher.
    workspace = page.get_by_label("Preview as team member")
    (workspace if workspace.count() else page.get_by_label("Portal preview role")).select_option(label=label)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


def test_every_module_and_core_workflows(page, server):
    # 1. Every sidebar module loads its page and data without errors.
    headings = {}
    for module in SIDEBAR:
        nav(page, module)
        headings[module] = page.locator("main h1").first.inner_text()
        no_error(page, module)
        if module == "Client portal":
            # The portal is a separate workspace with its own navigation.
            page.get_by_role("button", name="Onboard a client").first.wait_for()
            for tab in PORTAL_TABS:
                page.get_by_role("button", name=tab, exact=True).first.click()
                no_error(page, f"staff portal tab {tab}")
            page.get_by_role("button", name="← Back to internal workspace").click()
    for module in SIDEBAR:
        if module not in ("Overview", "Client portal"):
            assert headings[module] == module, headings
    assert "Workspace owner" in page.locator("body").inner_text()

    # 2. Create a company through the UI; it persists across a full page reload.
    nav(page, "Companies")
    page.get_by_role("button", name="New company").click()
    page.get_by_label("Company name *").fill("E2E Holdings")
    page.get_by_role("button", name="Create company").click()
    expect(page.get_by_role("button", name="Create company")).to_have_count(0)
    page.reload()
    nav(page, "Companies")
    page.locator("main").get_by_text("E2E Holdings").filter(visible=True).first.wait_for()
    no_error(page, "company create")

    # 3. Upload a document (multipart → /api/v2-files) and see it listed.
    nav(page, "Documents")
    page.get_by_role("button", name="Upload document").click()
    form = page.locator("form").filter(has=page.locator("input[type=file]"))
    form.get_by_label("Company *").select_option(label="E2E Holdings")
    form.locator("input[type=file]").set_input_files({"name": "board-minutes.txt", "mimeType": "text/plain", "buffer": b"Minutes"})
    form.get_by_role("button", name="Upload document").click()
    toast(page, "Document stored")
    page.locator("main").get_by_text("board-minutes.txt").filter(visible=True).first.wait_for()
    no_error(page, "upload")

    # 4. Create a task, edit it, and move it through the workflow.
    nav(page, "My work")
    page.get_by_role("button", name="New task").click()
    page.get_by_label("Title *").fill("E2E filing review")
    page.get_by_label("Company *").select_option(label="E2E Holdings")
    page.get_by_label("Due date *").fill("2026-12-01")
    page.get_by_label("Responsible person *").select_option(label="Workspace owner")
    page.get_by_role("button", name="Create task").click()
    expect(page.get_by_role("button", name="Create task")).to_have_count(0)
    page.locator("main").get_by_text("E2E filing review").filter(visible=True).first.click()
    page.get_by_role("button", name="Start work").click()
    page.get_by_role("button", name="Submit for review").click()
    expect(page.get_by_role("button", name="Submit for review")).to_have_count(0)
    no_error(page, "task workflow")
    close_details(page)
    expect(page.get_by_role("button", name="Close details")).to_have_count(0)

    # 5. Business rules surface in the UI: the secretary cannot approve work without evidence.
    preview_as(page, "Priya Sharma · Secretary")
    nav(page, "Review queue")
    page.locator("main").get_by_text("E2E filing review").filter(visible=True).first.click()
    page.get_by_role("button", name="Approve work").click()
    page.locator(".error-banner").get_by_text("Attach at least one supporting document.").wait_for()
    close_details(page)
    page.reload()  # the error was expected; start clean
    preview_as(page, "Workspace owner")

    # 6. Onboard a client through the portal, then use the portal as that client.
    nav(page, "Client portal")
    page.get_by_role("button", name="Onboard a client").first.click()
    page.get_by_label("Company / proposed company name *").fill("Portal Client Pvt Ltd")
    page.get_by_label("New client contact name (creates a local client profile) *").fill("Meera Client")
    page.get_by_role("button", name="Save draft").click()
    toast(page, "Saved successfully")
    no_error(page, "onboarding")
    page.get_by_role("button", name="← Back to internal workspace").click()
    preview_as(page, "Meera Client · Client")
    body = page.locator("body").inner_text()
    assert "Portal Client Pvt Ltd" in body
    assert page.locator("aside").get_by_role("button", name="Team & access", exact=True).count() == 0
    for tab in PORTAL_TABS:
        page.get_by_role("button", name=tab, exact=True).first.click()
        page.wait_for_load_state("networkidle")
        no_error(page, f"portal tab {tab}")

    # 7. Permissions are enforced by the server, not just hidden in the UI.
    statuses = page.evaluate("""async () => ({
        csv: (await fetch('/api/v2?export=csv')).status,
        member: (await fetch('/api/v2', {method: 'POST', headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({action: 'member', name: 'x', role: 'Secretary', companies: ['*']})})).status,
        state: await (await fetch('/api/v2')).json(),
    })""")
    assert statuses["csv"] == 403 and statuses["member"] == 403
    assert statuses["state"]["members"] == [] and statuses["state"]["audit"] == []
    assert "E2E filing review" not in str(statuses["state"])

    # 8. The session survives a reload (no silent fallback to the Owner).
    page.reload()
    page.wait_for_load_state("networkidle")
    assert "Meera Client" in page.locator("body").inner_text()
    preview_as(page, "Workspace owner")
    nav(page, "Audit trail")
    for entry in ("Created company", "Uploaded document", "Started client onboarding"):
        page.get_by_text(entry).first.wait_for()
    no_error(page, "audit trail")


def fill(page, fields: dict) -> None:
    for label, value in fields.items():
        field = page.get_by_label(label).first
        if field.evaluate("e => e.tagName") == "SELECT":
            field.select_option(label=value)
        else:
            field.fill(value)


def create_via_form(page, module: str, button: str, fields: dict, submit: str) -> None:
    nav(page, module)
    page.get_by_role("button", name=button).click()
    fill(page, fields)
    page.get_by_role("button", name=submit).click()
    expect(page.get_by_role("button", name=submit)).to_have_count(0)
    no_error(page, f"{module}: {button}")


def test_module_forms_edits_and_actions(page):
    common = {"Company *": "Forms Co", "Due date *": "2026-12-15", "Responsible person *": "Workspace owner"}
    create_via_form(page, "Companies", "New company", {"Company name *": "Forms Co"}, "Create company")

    # Board room: create, then edit through the details drawer; the edit persists after reload.
    create_via_form(page, "Board room", "New meeting", {"Title *": "Q4 board meeting", **common, "Minimum eligible attendees *": "1",
                                                         "Agenda": "Approve accounts"}, "Create meeting")
    page.locator("main").get_by_text("Q4 board meeting").filter(visible=True).first.click()
    page.get_by_label("Title *").first.fill("Q4 board meeting (edited)")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("main").get_by_text("Q4 board meeting (edited)").filter(visible=True).first).to_be_visible()
    close_details(page)
    page.reload()
    nav(page, "Board room")
    expect(page.locator("main").get_by_text("Q4 board meeting (edited)").filter(visible=True).first).to_be_visible()

    # Corporate event generates its ordered checklist tasks.
    create_via_form(page, "Corporate events", "New corporate event", {"Title *": "Appoint director", **common,
                    "Event type *": "Director appointment", "Effective date *": "2026-12-01"}, "Create corporate event")
    nav(page, "My work")
    expect(page.locator("main").get_by_text("Obtain consent and disclosures").filter(visible=True).first).to_be_visible()

    create_via_form(page, "Client requests", "New information request", {"Title *": "Bank statements Q3", **common}, "Create information request")
    create_via_form(page, "Rule library", "New recurring rule", {"Title *": "Monthly GST review", "Company *": "Forms Co",
                    "First anchor date *": "2026-12-01", "Responsible person *": "Workspace owner", "Frequency *": "Monthly",
                    "Days after anchor *": "10", "Source / internal policy reference *": "Internal policy GST-1"}, "Create recurring rule")
    create_via_form(page, "Submissions", "New submission", {"Title *": "MCA filing", **common, "Approved task *": "Obtain consent and disclosures"},
                    "Create submission")

    # Accounting: trial-balance CSV import (multipart) and a financial close.
    nav(page, "Accounting")
    page.get_by_role("button", name="Import CSV").click()
    form = page.locator("form").filter(has=page.locator("input[type=file]"))
    form.get_by_label("Company *").select_option(label="Forms Co")
    form.get_by_label("Period *").fill("2026-09")
    form.locator("input[type=file]").set_input_files({"name": "tb.csv", "mimeType": "text/csv", "buffer": b"account,debit,credit\nCash,500,0\nCapital,0,500"})
    form.get_by_role("button", name="Validate and import").click()
    toast(page, "Trial balance validated and imported")
    create_via_form(page, "Accounting", "New financial close", {"Title *": "September close", **common, "Accounting period *": "2026-09"},
                    "Create financial close")

    # Team & access: a new local profile becomes selectable in "Preview as".
    nav(page, "Team & access")
    page.get_by_role("button", name="Add local profile").click()
    page.get_by_label("Full name *").fill("Ravi Director")
    page.get_by_label("Role *").select_option(label="Director")
    page.get_by_label("Forms Co").check()
    page.get_by_role("button", name="Create member").click()
    expect(page.get_by_label("Preview as team member").locator("option", has_text="Ravi Director")).to_have_count(1)
    no_error(page, "team")

    # Briefings, reminders and report export.
    nav(page, "Briefings")
    page.get_by_role("button", name="Generate local briefing").click()
    expect(page.locator("main").get_by_text("Workspace briefing").filter(visible=True).first).to_be_visible()
    nav(page, "Inbox")
    page.get_by_role("button", name="Check reminders now").click()
    toast(page, "items created")
    nav(page, "Reports")
    with page.expect_download() as download:
        page.locator("main").get_by_text("Export").first.click()
    assert download.value.suggested_filename == "governance-report.csv"
    no_error(page, "briefings/inbox/reports")

    # Optional providers: the API reports AI as not configured, so the UI disables AI drafting.
    nav(page, "Briefings")
    expect(page.get_by_role("button", name="Generate AI draft")).to_be_disabled()


def test_client_answers_a_request_in_the_portal_and_staff_review_it(page):
    """Request → upload → review → correction → resubmission → close, through both real UIs."""
    title = "Q3 bank statements"
    row = lambda: page.locator("main").get_by_role("button", name=re.compile(re.escape(title))).first  # noqa: E731

    # The employee onboards a client and sends them a request from the internal Client requests module.
    nav(page, "Client portal")
    page.get_by_role("button", name="Onboard a client").first.click()
    page.get_by_label("Company / proposed company name *").fill("Requests Co Pvt Ltd")
    page.get_by_label("New client contact name (creates a local client profile) *").fill("Ravi Client")
    page.get_by_role("button", name="Save draft").click()
    toast(page, "Saved successfully")
    page.get_by_role("button", name="← Back to internal workspace").click()
    create_via_form(page, "Client requests", "New information request", {
        "Title *": title, "Company *": "Requests Co Pvt Ltd", "Due date *": "2026-12-15",
        "Client recipient *": "Ravi Client", "Requested information": "Upload the July–September statements."},
        "Create information request")

    # The client finds it under Requests (directly below Inbox), uploads the document and submits it.
    preview_as(page, "Ravi Client · Client")
    labels = page.locator("aside nav button").all_inner_texts()
    assert labels.index("Requests") == labels.index("Inbox") + 1, labels
    page.get_by_role("button", name="Requests", exact=True).first.click()
    expect(row()).to_contain_text("Awaiting Your Action")
    expect(row()).to_contain_text("Upload the July–September statements.")
    row().click()
    page.get_by_label("Document for this request").set_input_files({"name": "statements-q3.txt", "mimeType": "text/plain", "buffer": b"Q3"})
    page.get_by_role("button", name="Upload to this request").click()
    toast(page, "Document uploaded to this request")
    expect(page.get_by_role("dialog").get_by_role("link", name="statements-q3.txt")).to_be_visible()
    page.get_by_role("button", name="Submit for review").click()
    toast(page, "Submitted to your secretary for review")
    expect(page.get_by_role("dialog")).to_contain_text("Submitted. Your secretary is reviewing it")
    page.get_by_role("button", name="Close portal dialog").click()
    page.get_by_role("button", name="Work Tracker", exact=True).first.click()
    expect(row()).to_contain_text("Under Review")
    no_error(page, "client submits request")

    # The employee reviews the upload and asks for a correction.
    preview_as(page, "Workspace owner")
    nav(page, "Client requests")
    page.locator("main").get_by_text(title).filter(visible=True).first.click()
    expect(page.get_by_role("link", name=re.compile("statements-q3.txt"))).to_be_visible()
    page.get_by_label("Review note / correction requested").fill("September is missing.")
    page.get_by_role("button", name="Request correction").click()
    expect(page.locator(".drawer-meta")).to_contain_text("Returned")
    close_details(page)
    no_error(page, "staff returns request")

    # The client reads the correction and resubmits a corrected document.
    preview_as(page, "Ravi Client · Client")
    page.get_by_role("button", name="Requests", exact=True).first.click()
    page.get_by_role("button", name=re.compile("^Changes Requested")).click()
    expect(row()).to_contain_text("To correct: September is missing.")
    row().click()
    page.get_by_label("Document for this request").set_input_files({"name": "statements-q3-v2.txt", "mimeType": "text/plain", "buffer": b"Q3 + Sep"})
    page.get_by_role("button", name="Upload to this request").click()
    toast(page, "Document uploaded to this request")
    page.get_by_label(re.compile("^Message to your secretary")).fill("Added September.")
    page.get_by_role("button", name="Resubmit for review").click()
    toast(page, "Submitted to your secretary for review")
    page.get_by_role("button", name="Close portal dialog").click()
    no_error(page, "client resubmits request")

    # The employee sees both uploads and closes the request; both pages show it completed.
    preview_as(page, "Workspace owner")
    nav(page, "Client requests")
    page.locator("main").get_by_text(title).filter(visible=True).first.click()
    expect(page.get_by_role("link", name=re.compile("statements-q3-v2.txt"))).to_be_visible()
    page.get_by_role("button", name="Accept and close").click()
    expect(page.locator(".drawer-meta")).to_contain_text("Closed")
    close_details(page)
    preview_as(page, "Ravi Client · Client")
    page.get_by_role("button", name="Requests", exact=True).first.click()
    page.get_by_role("button", name=re.compile("^Completed")).click()
    expect(row()).to_contain_text("Completed")
    page.get_by_role("button", name="Work Tracker", exact=True).first.click()
    expect(row()).to_contain_text("Completed")
    no_error(page, "request closed")
