"""Browser test of password sign-in (GOVERNANCE_AUTH_MODE=password) through the real React UI.

Owner signs in, invites a team member from Team & access, the member activates the account from the
emailed link (development outbox), signs in with their own role, and signs out.

Run with:  GOVERNANCE_E2E=1 .venv/bin/python -m pytest tests/e2e -s
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import socket
from pathlib import Path

import pytest

from tests.e2e.test_react_frontend import BACKEND, _free_port, expect

pytestmark = pytest.mark.skipif(os.environ.get("GOVERNANCE_E2E") != "1", reason="set GOVERNANCE_E2E=1 to run the browser test")

OWNER_PASSWORD = "e2e owner pass phrase 2026"
MEMBER_PASSWORD = "e2e member pass phrase 2026"
BOOTSTRAP = """
from app.core.config import get_settings
from app.core.container import Services
from app.repositories.records import RecordRepository
from app.services.identity_service import add_member, create_organization, set_credentials
services = Services.build(get_settings())
with services.db.write() as session:
    _org, owner = create_organization(session, "firm-e2e", "E2E Associates", "Erin Owner")
    RecordRepository(session, "firm-e2e").insert("c1", "company", {"name": "Northwind Pvt Ltd"})
    RecordRepository(session, "firm-e2e").insert("sec", "member", {"name": "Sita Secretary", "role": "Secretary", "companies": ["*"], "active": True})
    add_member(session, "firm-e2e", "sec", "Sita Secretary", "Secretary", ["*"])
    set_credentials(session, owner.id, "erin@e2e.test", %r)
services.db.dispose()
""" % OWNER_PASSWORD


@pytest.fixture(scope="module")
def password_server(tmp_path_factory):
    data = tmp_path_factory.mktemp("e2e-password")
    port = _free_port()
    env = {**os.environ, "GOVERNANCE_ENVIRONMENT": "test", "GOVERNANCE_DATA_DIR": str(data), "GOVERNANCE_REMINDERS_ENABLED": "false",
           "GOVERNANCE_AUTH_MODE": "password", "GOVERNANCE_MAIL_PROVIDER": "outbox", "GOVERNANCE_PUBLIC_URL": f"http://127.0.0.1:{port}"}
    subprocess.run([sys.executable, "-c", BOOTSTRAP], cwd=BACKEND, env=env, check=True)
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                               cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", data / "mail-outbox"
    process.terminate()
    process.wait(timeout=10)


def watched(browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.problems = []
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    page.on("response", lambda r: page.problems.append(f"{r.status} {r.url}") if "/api/" in r.url and r.status >= 500 else None)
    page.on("console", lambda m: page.problems.append(m.text) if "Content Security Policy" in m.text else None)
    return page


def sign_in(page, base: str, email: str, password: str) -> None:
    page.goto(base + "/")
    page.get_by_role("heading", name="Sign in").wait_for()
    page.get_by_label("Email").fill(email)
    page.get_by_role("textbox", name="Password *", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_text("Good work starts with a clear view.").wait_for()


@pytest.mark.skip(reason="The Invite button is commented out in Team & access (accounts created "
                         "with a password sign in immediately and need no invite); re-enable this "
                         "test if that control is restored.")
def test_sign_in_invite_activate_and_sign_out(password_server):
    from playwright.sync_api import sync_playwright

    base, outbox = password_server
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        owner = watched(browser)
        # Wrong password: generic error, still on the sign-in screen.
        owner.goto(base + "/")
        owner.get_by_label("Email").fill("erin@e2e.test")
        owner.get_by_role("textbox", name="Password *", exact=True).fill("not the right password")
        owner.get_by_role("button", name="Sign in").click()
        expect(owner.get_by_role("alert")).to_contain_text("The email or password is incorrect.")
        sign_in(owner, base, "erin@e2e.test", OWNER_PASSWORD)
        expect(owner.locator(".account-strip")).to_contain_text("Erin Owner · Owner · erin@e2e.test")
        expect(owner.get_by_text("LOCAL PREVIEW")).to_have_count(0)
        expect(owner.locator(".workspace-chip")).to_contain_text("E2E Associates")

        # Invite the secretary from Team & access (the email address is entered in a prompt).
        owner.locator("nav").get_by_text("Team & access", exact=True).click()
        row = owner.locator("tr", has_text="Sita Secretary")
        owner.once("dialog", lambda dialog: dialog.accept("sita@e2e.test"))
        row.get_by_role("button", name="Invite").click()
        expect(row).to_contain_text("Invited")
        message = json.loads(sorted(outbox.glob("*.json"))[-1].read_text())
        assert message["to"] == "sita@e2e.test"
        link = re.search(r"http://\S+#activate\?token=[A-Za-z0-9_-]+", message["text"]).group(0)

        # The secretary activates the account in a separate browser context.
        member_context = browser.new_context(viewport={"width": 1400, "height": 900})
        member = member_context.new_page()
        member.goto(link)
        member.get_by_role("heading", name="Activate your account").wait_for()
        member.get_by_role("textbox", name="New password *", exact=True).fill(MEMBER_PASSWORD)
        member.get_by_role("textbox", name="Repeat password *", exact=True).fill(MEMBER_PASSWORD)
        member.get_by_role("button", name="Activate account").click()
        expect(member.get_by_role("status")).to_contain_text("Your account is active")
        member.get_by_label("Email").fill("sita@e2e.test")
        member.get_by_role("textbox", name="Password *", exact=True).fill(MEMBER_PASSWORD)
        member.get_by_role("button", name="Sign in").click()
        member.get_by_text("Good work starts with a clear view.").wait_for()
        expect(member.locator(".account-strip")).to_contain_text("Sita Secretary · Secretary")

        # The owner sees the account as active, then deactivates it: the secretary's next request fails.
        owner.reload()  # the app opens on Overview after a reload (existing behaviour)
        owner.locator("nav").get_by_text("Team & access", exact=True).click()
        owner.get_by_text("People & permissions").wait_for()
        expect(owner.locator("tr", has_text="Sita Secretary")).to_contain_text("Active")
        owner.locator("tr", has_text="Sita Secretary").get_by_role("button", name="Deactivate").click()
        expect(owner.locator("tr", has_text="Sita Secretary")).to_contain_text("Deactivated")
        assert member.evaluate("fetch('/api/v2').then(r => r.status)") == 401

        # Sign out returns to the sign-in screen and the session no longer works.
        owner.get_by_role("button", name="Sign out", exact=True).click()
        owner.get_by_role("heading", name="Sign in").wait_for()
        assert owner.evaluate("fetch('/api/v2').then(r => r.status)") == 401
        assert not owner.problems and not getattr(member, "problems", [])
        browser.close()


def test_owner_creates_member_with_a_working_password(password_server):
    """Team & access: Owner sets a member's password directly (no email delivery needed); the new
    member signs in with it immediately — no forced change, no extra step."""
    from playwright.sync_api import sync_playwright

    base, _outbox = password_server
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        owner = watched(browser)
        sign_in(owner, base, "erin@e2e.test", OWNER_PASSWORD)

        owner.locator("nav").get_by_text("Team & access", exact=True).click()
        owner.get_by_role("button", name="Add team profile").click()
        owner.get_by_label("Full name").fill("Priya New Hire")
        owner.get_by_label("Email address").fill("priya@e2e.test")
        owner.get_by_role("textbox", name="Password *", exact=True).fill("a starter owned pass phrase")
        owner.get_by_role("textbox", name="Confirm password").fill("a starter owned pass phrase")
        owner.get_by_label("Role").select_option("Accountant")
        owner.get_by_role("checkbox", name="Northwind Pvt Ltd").check()
        owner.get_by_role("button", name="Create member").click()
        expect(owner.get_by_text("Priya New Hire")).to_be_visible()

        # The new member signs in with that password immediately — straight to the workspace.
        member_context = browser.new_context(viewport={"width": 1400, "height": 900})
        member = member_context.new_page()
        member.goto(base + "/")
        member.get_by_role("heading", name="Sign in").wait_for()
        member.get_by_label("Email").fill("priya@e2e.test")
        member.get_by_role("textbox", name="Password *", exact=True).fill("a starter owned pass phrase")
        member.get_by_role("button", name="Sign in").click()
        member.get_by_text("Good work starts with a clear view.").wait_for()
        expect(member.locator(".account-strip")).to_contain_text("Priya New Hire · Accountant")

        # No forced-change gate: every ordinary endpoint already works.
        allowed_status = member.evaluate("fetch('/api/v2').then(r => r.status)")
        assert allowed_status == 200

        # The password is wrong only when it's actually wrong.
        member_context.close()
        retry = browser.new_context(viewport={"width": 1400, "height": 900})
        retry_page = retry.new_page()
        retry_page.goto(base + "/")
        retry_page.get_by_role("heading", name="Sign in").wait_for()
        retry_page.get_by_label("Email").fill("priya@e2e.test")
        retry_page.get_by_role("textbox", name="Password *", exact=True).fill("the wrong phrase entirely")
        retry_page.get_by_role("button", name="Sign in").click()
        expect(retry_page.get_by_role("alert")).to_contain_text("The email or password is incorrect.")
        retry_page.get_by_role("textbox", name="Password *", exact=True).fill("a starter owned pass phrase")
        retry_page.get_by_role("button", name="Sign in").click()
        retry_page.get_by_text("Good work starts with a clear view.").wait_for()
        assert not owner.problems
        browser.close()
