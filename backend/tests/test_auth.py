"""Phase 3: built-in password authentication, sessions, CSRF, throttling and account administration."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api.deps import services_of
from app.auth import csrf_for, issue_session
from app.core.config import Settings
from app.main import create_app
from app.models import AuthSession, AuthToken, LoginAttempt, SecurityEvent, User
from app.repositories.identity import IdentityRepository
from app.repositories.records import RecordRepository
from app.repositories.sessions import iso_in, token_hash
from app.services.identity_service import add_member, create_organization, set_credentials
from tests.conftest import ANONYMOUS_CSRF, Api, browser_headers

SESSION = "gov_session"
OWNER_PASSWORD = "correct horse battery staple"
SECRETARY_PASSWORD = "secretary pass phrase 42"


@pytest.fixture
def password_settings(settings: Settings) -> Settings:
    settings.governance_auth_mode = "password"
    return settings


@pytest.fixture
def client(password_settings: Settings):
    with TestClient(create_app(password_settings), base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def firm(client):
    """Firm A: Owner (with credentials), a Secretary and a Client of company c1 (no credentials yet)."""
    services = services_of(client)
    with services.db.write() as session:
        _org, owner = create_organization(session, "firm-a", "Firm A", "Asha Owner")
        RecordRepository(session, "firm-a").insert("c1", "company", {"name": "Company One"})
        RecordRepository(session, "firm-a").insert("c2", "company", {"name": "Company Two"})
        for member_id, name, role, companies in (("sec", "Sam Secretary", "Secretary", ["*"]), ("cli", "Cleo Client", "Client", ["c1"])):
            RecordRepository(session, "firm-a").insert(member_id, "member", {"name": name, "role": role, "companies": companies, "active": True})
            add_member(session, "firm-a", member_id, name, role, companies)
        set_credentials(session, owner.id, "owner@firm-a.test", OWNER_PASSWORD)
    return services


def anon(client) -> Api:
    return Api(client, cookie_name=SESSION)


def login(client, email: str, password: str, organization: str | None = None, expect: int = 200) -> Api:
    api = anon(client)
    response = api.request("POST", "/api/auth/login", json={"email": email, "password": password, **({"organization": organization} if organization else {})})
    assert response.status_code == expect, response.text
    if expect == 200 and response.json().get("ok"):
        api.cookie = response.cookies.get(SESSION)
        assert api.cookie
    return api


def outbox(services) -> list[dict]:
    folder: Path = services.settings.outbox_dir
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def link_token(message: dict, kind: str) -> str:
    return re.search(rf"#{kind}\?token=([A-Za-z0-9_-]+)", message["text"]).group(1)


def events(services, name: str) -> list[SecurityEvent]:
    with services.db.read() as session:
        return list(session.scalars(select(SecurityEvent).where(SecurityEvent.event == name)))


# Sign-in ---------------------------------------------------------------------------------------

def test_password_mode_has_no_preview_sign_in(client, firm):
    page = client.get("/")
    assert "gov_preview" not in page.cookies and SESSION not in page.cookies
    assert page.cookies.get("gov_csrf")  # the sign-in form's anonymous CSRF token
    api = anon(client)
    assert api.request("GET", "/api/v2").status_code == 401
    assert api.request("POST", "/api/session", json={"actor": "owner"}).status_code == 404  # no preview switching
    assert api.request("GET", "/api/auth/config").json() == {"mode": "password"}


def test_sign_in_issues_a_secure_session_bound_to_the_membership(client, firm):
    api = anon(client)
    response = api.request("POST", "/api/auth/login", json={"email": "OWNER@firm-a.test ", "password": OWNER_PASSWORD})
    assert response.status_code == 200 and response.json() == {"ok": True}
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION}="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie and "Path=/" in session_cookie
    csrf_cookie = next(c for c in cookies if c.startswith("gov_csrf="))
    assert "HttpOnly" not in csrf_cookie
    api.cookie = response.cookies.get(SESSION)
    me = api.call("/api/auth/me")
    assert me["organization"] == {"id": "firm-a", "name": "Firm A"}
    assert me["membership"]["role"] == "Owner" and me["user"]["email"] == "owner@firm-a.test"
    assert api.state()["actor"]["role"] == "Owner"
    with firm.db.read() as session:
        row = session.get(AuthSession, token_hash(api.cookie))
        assert row.method == "password" and row.idle_expires and row.user_id == me["user"]["id"]
        assert session.get(User, me["user"]["id"]).last_sign_in_at
    assert len(events(firm, "sign_in")) == 1


@pytest.mark.parametrize("email,password", [
    ("owner@firm-a.test", "wrong password entirely"), ("nobody@firm-a.test", OWNER_PASSWORD), ("not-an-email", OWNER_PASSWORD),
    ("owner@firm-a.test", ""), ("owner@firm-a.test", None),
])
def test_failed_sign_in_is_generic_and_recorded(client, firm, email, password):
    response = anon(client).request("POST", "/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json() == {"error": "The email or password is incorrect."}
    assert SESSION not in response.cookies
    assert len(events(firm, "sign_in_failed")) == 1


def test_sign_in_is_throttled_per_account(client, firm):
    for _ in range(5):
        login(client, "owner@firm-a.test", "wrong password entirely", expect=401)
    # Even the right password is refused while the account is throttled.
    response = anon(client).request("POST", "/api/auth/login", json={"email": "owner@firm-a.test", "password": OWNER_PASSWORD})
    assert response.status_code == 429
    assert events(firm, "sign_in_throttled")
    # The window passes: failures older than the window no longer count.
    with firm.db.write() as session:
        session.execute(update(LoginAttempt).values(at=iso_in(-3600)))
    login(client, "owner@firm-a.test", OWNER_PASSWORD)


def test_sign_in_is_throttled_per_client_address(client, firm):
    firm.settings.governance_login_max_failures_per_client = 3
    for n in range(3):
        login(client, f"user{n}@elsewhere.test", "wrong password entirely", expect=401)
    response = anon(client).request("POST", "/api/auth/login", json={"email": "owner@firm-a.test", "password": OWNER_PASSWORD})
    assert response.status_code == 429


def test_successful_sign_in_clears_the_account_failures(client, firm):
    for _ in range(4):
        login(client, "owner@firm-a.test", "wrong password entirely", expect=401)
    login(client, "owner@firm-a.test", OWNER_PASSWORD)
    for _ in range(4):
        login(client, "owner@firm-a.test", "wrong password entirely", expect=401)
    login(client, "owner@firm-a.test", OWNER_PASSWORD)


# CSRF ------------------------------------------------------------------------------------------

def test_writes_require_the_session_csrf_token(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    body = {"action": "create", "kind": "company", "data": {"name": "New Co"}}
    # No header at all: refused by the double-submit check.
    no_header = {"cookie": f"{SESSION}={owner.cookie}; gov_csrf={csrf_for(owner.cookie)}"}
    assert client.post("/api/v2", json=body, headers=no_header).status_code == 403
    # A matching cookie/header pair that is not the session's token: refused by the session binding.
    forged = {"cookie": f"{SESSION}={owner.cookie}; gov_csrf=forged", "x-csrf-token": "forged"}
    response = client.post("/api/v2", json=body, headers=forged)
    assert response.status_code == 403 and "security token" in response.json()["error"]
    # Header without the cookie (another site cannot read or set it): refused.
    assert client.post("/api/v2", json=body, headers={"cookie": f"{SESSION}={owner.cookie}", "x-csrf-token": csrf_for(owner.cookie)}).status_code == 403
    assert owner.v2(body)["ok"] is True


def test_sign_in_form_requires_a_csrf_pair(client, firm):
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": "owner@firm-a.test", "password": OWNER_PASSWORD})
    assert response.status_code == 403
    response = client.post("/api/auth/login", json={"email": "owner@firm-a.test", "password": OWNER_PASSWORD},
                           headers={"cookie": f"gov_csrf={ANONYMOUS_CSRF}", "x-csrf-token": ANONYMOUS_CSRF})
    assert response.status_code == 200


def test_page_load_repairs_a_missing_csrf_cookie_for_the_session(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    client.cookies.clear()
    page = client.get("/", headers={"cookie": f"{SESSION}={owner.cookie}"})
    assert page.cookies.get("gov_csrf") == csrf_for(owner.cookie)


# Sessions: expiry, revocation, preview sessions --------------------------------------------------

def test_sign_out_revokes_the_session_server_side(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    response = owner.request("POST", "/api/auth/logout", json={})
    assert response.status_code == 200
    assert any(c.startswith(f"{SESSION}=") and "Max-Age=0" in c for c in response.headers.get_list("set-cookie"))
    # The old cookie replayed after sign-out is dead.
    assert owner.request("GET", "/api/v2").status_code == 401
    assert events(firm, "sign_out")


def test_sign_out_everywhere_and_session_list(client, firm):
    first = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    second = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    listed = first.call("/api/auth/sessions")["items"]
    assert len(listed) == 2 and sum(1 for s in listed if s["current"]) == 1
    assert all("token" not in json.dumps(s).lower() for s in listed)
    other = next(s for s in listed if not s["current"])
    first.call("/api/auth/sessions/revoke", {"id": other["id"]})
    assert second.request("GET", "/api/v2").status_code == 401
    third = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    assert first.call("/api/auth/logout", {"everywhere": True})["sessionsEnded"] == 2
    assert first.request("GET", "/api/v2").status_code == 401 and third.request("GET", "/api/v2").status_code == 401


def test_cannot_revoke_another_users_session(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        set_credentials(session, IdentityRepository(session).membership_by_member("firm-a", "sec").id, "sec@firm-a.test", SECRETARY_PASSWORD)
    secretary = login(client, "sec@firm-a.test", SECRETARY_PASSWORD)
    owners_session = owner.call("/api/auth/sessions")["items"][0]["id"]
    secretary.call("/api/auth/sessions/revoke", {"id": owners_session}, expect=404)
    assert owner.request("GET", "/api/v2").status_code == 200


def test_expired_and_idle_sessions_are_rejected(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        session.execute(update(AuthSession).where(AuthSession.token_hash == token_hash(owner.cookie)).values(expires=iso_in(-1)))
    assert owner.request("GET", "/api/v2").status_code == 401
    idle = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        session.execute(update(AuthSession).where(AuthSession.token_hash == token_hash(idle.cookie)).values(idle_expires=iso_in(-1)))
    assert idle.request("GET", "/api/v2").status_code == 401


def test_activity_extends_the_idle_timeout(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    soon = iso_in(120)
    with firm.db.write() as session:
        session.execute(update(AuthSession).where(AuthSession.token_hash == token_hash(owner.cookie))
                        .values(idle_expires=soon, last_seen_at=iso_in(-300)))
    assert owner.request("GET", "/api/v2").status_code == 200
    with firm.db.read() as session:
        assert session.get(AuthSession, token_hash(owner.cookie)).idle_expires > soon


def test_preview_sessions_are_worthless_in_password_mode(client, firm):
    with firm.db.read() as session:
        owner = IdentityRepository(session).membership_by_member("firm-a", "owner")
    token = issue_session(firm, owner.id, method="preview")
    for cookie_name in (SESSION, "gov_preview"):
        api = Api(client, cookie_name=cookie_name)
        api.cookie = token
        assert api.request("GET", "/api/v2").status_code == 401


def test_forged_and_tampered_cookies_are_rejected(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    for value in ("forged", owner.cookie[:-1] + ("A" if owner.cookie[-1] != "A" else "B"), token_hash(owner.cookie)):
        api = anon(client)
        api.cookie = value
        assert api.request("GET", "/api/v2").status_code == 401


# Account lifecycle -----------------------------------------------------------------------------

def test_invitation_activation_and_first_sign_in(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    assert owner.call("/api/auth/team/invite", {"member": "sec", "email": "Sam@Firm-A.test"}) == {"ok": True, "delivered": True}
    message = outbox(firm)[-1]
    assert message["to"] == "sam@firm-a.test" and message["template"] == "activation"
    token = link_token(message, "activate")
    with firm.db.read() as session:  # only the hash is stored
        assert session.get(AuthToken, token) is None and session.get(AuthToken, token_hash(token)) is not None
    public = anon(client)
    public.call("/api/auth/activate", {"token": token, "password": "short"}, expect=400)
    public.call("/api/auth/activate", {"token": token, "password": "sam secretary password"}, expect=400)  # contains the name
    public.call("/api/auth/activate", {"token": token, "password": SECRETARY_PASSWORD})
    public.call("/api/auth/activate", {"token": token, "password": SECRETARY_PASSWORD}, expect=400)  # single use
    secretary = login(client, "sam@firm-a.test", SECRETARY_PASSWORD)
    assert secretary.call("/api/auth/me")["membership"]["role"] == "Secretary"
    team = {m["member"]: m for m in owner.call("/api/auth/team")["items"]}
    assert team["sec"]["activated"] is True and team["cli"]["activated"] is False
    owner.call("/api/auth/team/invite", {"member": "sec", "email": "sam@firm-a.test"}, expect=409)  # already active
    owner.call("/api/auth/team/invite", {"member": "cli", "email": "owner@firm-a.test"}, expect=409)  # email taken


def test_activation_links_expire(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    owner.call("/api/auth/team/invite", {"member": "sec", "email": "sam@firm-a.test"})
    token = link_token(outbox(firm)[-1], "activate")
    with firm.db.write() as session:
        session.execute(update(AuthToken).values(expires_at=iso_in(-1)))
    anon(client).call("/api/auth/activate", {"token": token, "password": SECRETARY_PASSWORD}, expect=400)


def test_password_reset_flow(client, firm):
    signed_in = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    public = anon(client)
    before = len(outbox(firm))
    unknown = public.call("/api/auth/password/forgot", {"email": "nobody@firm-a.test"})
    known = public.call("/api/auth/password/forgot", {"email": "owner@firm-a.test"})
    assert unknown == known  # no account enumeration
    messages = outbox(firm)[before:]
    assert len(messages) == 1 and messages[0]["to"] == "owner@firm-a.test"
    token = link_token(messages[0], "reset-password")
    public.call("/api/auth/password/reset", {"token": token, "password": "weak"}, expect=400)
    public.call("/api/auth/password/reset", {"token": token, "password": "a brand new pass phrase"})
    public.call("/api/auth/password/reset", {"token": token, "password": "another new pass phrase"}, expect=400)
    assert signed_in.request("GET", "/api/v2").status_code == 401  # every session ended
    login(client, "owner@firm-a.test", OWNER_PASSWORD, expect=401)
    login(client, "owner@firm-a.test", "a brand new pass phrase")
    assert events(firm, "password_reset_completed")


def test_password_reset_requests_are_throttled(client, firm):
    public = anon(client)
    for _ in range(8):
        public.call("/api/auth/password/forgot", {"email": "owner@firm-a.test"})
    assert len([m for m in outbox(firm) if m["template"] == "password_reset"]) == 5
    assert events(firm, "password_reset_throttled")


def test_change_password_keeps_this_session_and_ends_others(client, firm):
    current = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    other = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    current.call("/api/auth/password/change", {"current": "wrong", "password": "a brand new pass phrase"}, expect=400)
    current.call("/api/auth/password/change", {"current": OWNER_PASSWORD, "password": "a brand new pass phrase"})
    assert current.request("GET", "/api/v2").status_code == 200
    assert other.request("GET", "/api/v2").status_code == 401


def test_passwords_are_stored_only_as_argon2id_hashes(client, firm):
    with firm.db.read() as session:
        stored = session.scalars(select(User.password_hash).where(User.email == "owner@firm-a.test")).one()
    assert stored.startswith("$argon2id$") and OWNER_PASSWORD not in stored


def test_deactivation_ends_sessions_and_blocks_sign_in(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        set_credentials(session, IdentityRepository(session).membership_by_member("firm-a", "sec").id, "sec@firm-a.test", SECRETARY_PASSWORD)
    secretary = login(client, "sec@firm-a.test", SECRETARY_PASSWORD)
    owner.call("/api/auth/team/status", {"member": "sec", "status": "inactive"})
    assert secretary.request("GET", "/api/v2").status_code == 401
    response = anon(client).request("POST", "/api/auth/login", json={"email": "sec@firm-a.test", "password": SECRETARY_PASSWORD})
    assert response.status_code == 403
    profile = next(m for m in owner.state()["members"] if m["id"] == "sec")
    assert profile["active"] is False
    owner.call("/api/auth/team/status", {"member": "sec", "status": "active"})
    login(client, "sec@firm-a.test", SECRETARY_PASSWORD)
    assert [e.membership_id for e in events(firm, "member_deactivated")]


def test_owner_cannot_lock_the_workspace_out(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    owner.call("/api/auth/team/status", {"member": "owner", "status": "inactive"}, expect=400)  # self


# Multi-company assignment ------------------------------------------------------------------------

def test_owner_assigns_multiple_companies_to_one_employee(client, firm):
    """A Secretary scoped to one org-wide role (``*``) can be edited down to an explicit set of
    companies, grown, shrunk, and the live effect is immediate and exactly scoped."""
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        RecordRepository(session, "firm-a").insert("c3", "company", {"name": "Company Three"})
    with firm.db.write() as session:
        set_credentials(session, IdentityRepository(session).membership_by_member("firm-a", "sec").id,
                        "sec@firm-a.test", SECRETARY_PASSWORD)
    sec = login(client, "sec@firm-a.test", SECRETARY_PASSWORD)
    assert {r["id"] for r in sec.state()["records"] if r["kind"] == "company"} == {"c1", "c2", "c3"}  # was "*"

    # Owner assigns exactly two of the three companies.
    result = owner.call("/api/auth/team/companies", {"member": "sec", "companies": ["c1", "c2"]})
    assert sorted(result["companies"]) == ["c1", "c2"]
    profile = next(m for m in owner.state()["members"] if m["id"] == "sec")
    assert sorted(profile["companies"]) == ["c1", "c2"]

    sec = login(client, "sec@firm-a.test", SECRETARY_PASSWORD)
    assert {r["id"] for r in sec.state()["records"] if r["kind"] == "company"} == {"c1", "c2"}  # c3 is gone

    # Shrink to one company: access to the removed one is revoked immediately.
    owner.call("/api/auth/team/companies", {"member": "sec", "companies": ["c2"]})
    assert sec.request("GET", "/api/v2").status_code == 200  # same session, no re-login needed
    assert {r["id"] for r in sec.state()["records"] if r["kind"] == "company"} == {"c2"}

    # Grow again, including a foreign-organization id that must be silently dropped, not granted.
    with firm.db.write() as session:
        create_organization(session, "firm-outside-2", "Firm Outside Two", "Out Owner")
        RecordRepository(session, "firm-outside-2").insert("outside-co-2", "company", {"name": "Outside Co 2"})
    owner.call("/api/auth/team/companies", {"member": "sec", "companies": ["c1", "c2", "c3", "outside-co-2"]})
    profile = next(m for m in owner.state()["members"] if m["id"] == "sec")
    assert sorted(profile["companies"]) == ["c1", "c2", "c3"]

    # Only the Owner may do this; not the member itself, not another non-owner.
    sec.call("/api/auth/team/companies", {"member": "sec", "companies": ["c1"]}, expect=403)
    owner.call("/api/auth/team/companies", {"member": "owner", "companies": ["c1"]}, expect=400)  # self
    owner.call("/api/auth/team/companies", {"member": "sec", "companies": []}, expect=400)  # must keep at least one


# Real Client accounts ----------------------------------------------------------------------------

def test_new_client_account_full_lifecycle(client, firm):
    """Owner creates a real Client account (email + password) through the ordinary
    member-creation action. The Client can sign in immediately with that password — no forced
    change — then signs in and out independently like any other account, scoped to only its own
    company."""
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    created = owner.v2({
        "action": "member", "name": "Cara Client", "role": "Client", "companies": ["c2"],
        "email": "cara@firm-a.test", "password": "lighthouse autumn meadow", "confirmPassword": "lighthouse autumn meadow",
    })
    assert created["id"]

    # Signs in immediately with the password the Owner set — a normal, server-validated session.
    cara = login(client, "cara@firm-a.test", "lighthouse autumn meadow")
    me = cara.call("/api/auth/me")
    assert me["mustChangePassword"] is False and me["membership"]["role"] == "Client"
    assert cara.request("GET", "/api/v2").status_code == 200
    assert cara.request("GET", "/api/portal").status_code == 200

    # Scoped to its own company only: nothing from c1, no staff roster, no audit trail.
    state = cara.state()
    assert state["members"] == [] and state["audit"] == []
    assert {r["id"] for r in state["records"] if r["kind"] == "company"} == {"c2"}
    assert [c["id"] for c in cara.call("/api/portal")["companies"]] == ["c2"]

    # Never receives staff/Owner permissions.
    cara.v2({"action": "member", "name": "Mallory", "role": "Owner", "companies": ["*"]}, 403)
    cara.call("/api/auth/team", expect=403)

    # Signs out independently; the session is then invalid everywhere.
    cara.call("/api/auth/logout", {})
    assert cara.request("GET", "/api/v2").status_code == 401


def test_company_and_client_credentials_are_created_together(client, firm):
    """Onboarding a brand-new company can also set up the client's sign-in in the same step: one
    action, one transaction. The resulting account works exactly like one made from Team & Access,
    and a rejected password leaves no partial company or client profile behind."""
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)

    # Mismatched passwords: nothing is written — no company, no client profile, no user.
    before_companies = {r["id"] for r in owner.state()["records"] if r["kind"] == "company"}
    owner.portal({
        "action": "companyCreate", "data": {"name": "Should not exist"}, "clientName": "Rejected Client",
        "clientEmail": "rejected@firm-a.test", "clientPassword": "one phrase here", "clientConfirmPassword": "a different phrase",
    }, 400)
    assert {r["id"] for r in owner.state()["records"] if r["kind"] == "company"} == before_companies
    assert not any(m["name"] == "Rejected Client" for m in owner.state()["members"])

    created = owner.portal({
        "action": "companyCreate", "data": {"name": "Newly onboarded Pvt Ltd"}, "clientName": "Nora Newclient",
        "clientEmail": "nora@firm-a.test", "clientPassword": "quartz meridian orchard", "clientConfirmPassword": "quartz meridian orchard",
    })
    company_id = created["id"]
    assert company_id not in before_companies

    nora = login(client, "nora@firm-a.test", "quartz meridian orchard")
    me = nora.call("/api/auth/me")
    assert me["mustChangePassword"] is False and me["membership"]["role"] == "Client"
    state = nora.state()
    assert {r["id"] for r in state["records"] if r["kind"] == "company"} == {company_id}
    assert [c["id"] for c in nora.call("/api/portal")["companies"]] == [company_id]

    # A company onboarded without an email still works exactly as before (name-only, no credentials).
    name_only = owner.portal({"action": "companyCreate", "data": {"name": "Name only Pvt Ltd"}, "clientName": "Uncredentialed Client"})["id"]
    uncredentialed = next(m for m in owner.state()["members"] if m["name"] == "Uncredentialed Client")
    assert uncredentialed["companies"] == [name_only]
    owner.call("/api/auth/team/invite", {"member": uncredentialed["id"], "email": "later@firm-a.test"})


# Privilege escalation ---------------------------------------------------------------------------

def test_only_owners_administer_accounts(client, firm):
    with firm.db.write() as session:
        repo = IdentityRepository(session)
        set_credentials(session, repo.membership_by_member("firm-a", "sec").id, "sec@firm-a.test", SECRETARY_PASSWORD)
        set_credentials(session, repo.membership_by_member("firm-a", "cli").id, "cli@firm-a.test", "client pass phrase 77")
    for email, password in (("sec@firm-a.test", SECRETARY_PASSWORD), ("cli@firm-a.test", "client pass phrase 77")):
        api = login(client, email, password)
        api.call("/api/auth/team/invite", {"member": "owner", "email": "x@firm-a.test"}, expect=403)
        api.call("/api/auth/team/status", {"member": "owner", "status": "inactive"}, expect=403)
        api.call("/api/auth/team", expect=403)
        api.call("/api/auth/security-events", expect=403)
        api.v2({"action": "member", "name": "Mallory", "role": "Owner", "companies": ["*"]}, 403)


def test_browser_supplied_identity_is_ignored(client, firm):
    with firm.db.write() as session:
        set_credentials(session, IdentityRepository(session).membership_by_member("firm-a", "cli").id, "cli@firm-a.test", "client pass phrase 77")
    client_api = login(client, "cli@firm-a.test", "client pass phrase 77")
    # Role, organization and actor fields in the body or headers do not change who is acting.
    response = client_api.request("GET", "/api/v2", headers={"x-organization": "firm-b", "x-role": "Owner"})
    assert response.json()["actor"]["role"] == "Client"
    client_api.v2({"action": "create", "kind": "company", "data": {"name": "X"}, "role": "Owner", "actor": "owner"}, 403)
    assert {r["id"] for r in client_api.state()["records"] if r["kind"] == "company"} == {"c1"}
    client_api.call("/api/session", {"actor": "owner"}, expect=404)  # no profile switching in password mode


def test_organization_choice_is_limited_to_the_users_memberships(client, firm):
    with firm.db.write() as session:
        _org, owner_b = create_organization(session, "firm-b", "Firm B", "Bo Owner")
        _org, owner_c = create_organization(session, "firm-c", "Firm C", "Cy Owner")
        # The firm-a owner also gets a membership in firm-b (same user).
        user_id = IdentityRepository(session).membership_by_member("firm-a", "owner").user_id
        from app.models import Membership
        session.execute(update(Membership).where(Membership.id == owner_b.id).values(user_id=user_id))
    choose = anon(client).call("/api/auth/login", {"email": "owner@firm-a.test", "password": OWNER_PASSWORD})
    assert choose["ok"] is False and {o["id"] for o in choose["chooseOrganization"]} == {"firm-a", "firm-b"}
    login(client, "owner@firm-a.test", OWNER_PASSWORD, organization="firm-c", expect=403)
    in_b = login(client, "owner@firm-a.test", OWNER_PASSWORD, organization="firm-b")
    assert in_b.call("/api/auth/me")["organization"]["id"] == "firm-b"
    assert not [r for r in in_b.state()["records"] if r.get("company") in ("c1", "c2") or r["id"] in ("c1", "c2")]


def test_security_events_are_organization_scoped(client, firm):
    login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        _org, owner_b = create_organization(session, "firm-b", "Firm B", "Bo Owner")
        set_credentials(session, owner_b.id, "owner@firm-b.test", "second firm pass phrase")
    owner_b_api = login(client, "owner@firm-b.test", "second firm pass phrase")
    items = owner_b_api.call("/api/auth/security-events")["items"]
    assert items and all(e["membership"] == owner_b.id for e in items if e["membership"])
    owner_a = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    assert all(e["membership"] != owner_b.id for e in owner_a.call("/api/auth/security-events")["items"])


def test_audit_records_authentication_events(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    actions = [a["action"] for a in owner.state()["audit"]]
    assert "Signed in · Asha Owner" in actions


@pytest.mark.postgres
def test_deployed_password_mode_uses_host_prefixed_secure_cookies(tmp_path, postgres_url):
    from tests.test_foundation import deployed, migrated

    settings = migrated(deployed(tmp_path, governance_database_url=postgres_url))
    with TestClient(create_app(settings), base_url="https://testserver") as test_client:
        services = services_of(test_client)
        with services.db.write() as session:
            _org, owner = create_organization(session, "firm-p", "Firm P", "Pat Owner")
            set_credentials(session, owner.id, "pat@firm-p.test", OWNER_PASSWORD)
        page = test_client.get("/")
        csrf = page.cookies.get("__Host-gov_csrf")
        assert csrf
        response = test_client.post("/api/auth/login", json={"email": "pat@firm-p.test", "password": OWNER_PASSWORD},
                                    headers={"x-csrf-token": csrf})
        assert response.status_code == 200, response.text
        session_cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("__Host-gov_session="))
        assert "Secure" in session_cookie and "HttpOnly" in session_cookie and "Domain" not in session_cookie
        assert test_client.post("/api/session", json={"actor": "owner"},
                                headers={"x-csrf-token": test_client.cookies.get("__Host-gov_csrf")}).status_code == 404


def test_browser_headers_helper_matches_the_server_rule():
    headers = browser_headers("POST", "abc")
    assert headers["x-csrf-token"] == csrf_for("abc") and f"gov_csrf={csrf_for('abc')}" in headers["cookie"]


def test_demo_team_is_never_seeded_into_a_real_organization(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    before = {m["id"] for m in owner.state()["members"]}
    owner.v2({"action": "initialise"})
    assert {m["id"] for m in owner.state()["members"]} == before == {"owner", "sec", "cli"}


# Member passwords: no forced change (Owner creates a member directly) --------------------------

def test_owner_creates_member_with_a_working_password(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    created = owner.v2({"action": "member", "name": "Nora New", "role": "Accountant", "companies": ["c1"],
                        "email": "nora@firm-a.test", "password": "a starter pass phrase", "confirmPassword": "a starter pass phrase"})
    assert created["ok"] is True
    with firm.db.read() as session:
        user = session.scalars(select(User).where(User.email == "nora@firm-a.test")).one()
    assert user.password_hash and user.password_hash.startswith("$argon2id$") and user.must_change_password is False


def test_new_password_signs_in_and_works_immediately(client, firm):
    """No forced change: the password the Owner sets is the member's real password from the
    first sign-in onward. They can still change it voluntarily any time."""
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    owner.v2({"action": "member", "name": "Nora New", "role": "Accountant", "companies": ["c1"],
             "email": "nora@firm-a.test", "password": "a starter pass phrase", "confirmPassword": "a starter pass phrase"})
    nora = login(client, "nora@firm-a.test", "a starter pass phrase")
    assert nora.call("/api/auth/me")["mustChangePassword"] is False
    for method, path in (("GET", "/api/v2"), ("GET", "/api/portal"), ("GET", "/api/v2/records")):
        assert nora.request(method, path).status_code == 200
    nora.call("/api/auth/password/change", {"current": "a starter pass phrase", "password": "a completely different phrase"})
    login(client, "nora@firm-a.test", "a starter pass phrase", expect=401)
    login(client, "nora@firm-a.test", "a completely different phrase")


def test_new_password_must_meet_the_policy_and_match(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    owner.v2({"action": "member", "name": "Weak", "role": "Accountant", "companies": ["c1"],
             "email": "weak@firm-a.test", "password": "short", "confirmPassword": "short"}, 400)
    owner.v2({"action": "member", "name": "Mismatch", "role": "Accountant", "companies": ["c1"],
             "email": "mismatch@firm-a.test", "password": "a starter pass phrase", "confirmPassword": "a different phrase entirely"}, 400)
    owner.v2({"action": "member", "name": "No Email", "role": "Accountant", "companies": ["c1"],
             "password": "a starter pass phrase", "confirmPassword": "a starter pass phrase"}, 400)


def test_new_account_email_must_be_unique_and_creation_is_atomic(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    before = {m["id"] for m in owner.state()["members"]}
    owner.v2({"action": "member", "name": "Dup", "role": "Accountant", "companies": ["c1"],
             "email": "owner@firm-a.test", "password": "a starter pass phrase", "confirmPassword": "a starter pass phrase"}, 409)
    # Nothing was partially created: no new member, no new user with that failed attempt's name.
    assert {m["id"] for m in owner.state()["members"]} == before
    with firm.db.read() as session:
        assert session.scalars(select(User).where(User.display_name == "Dup")).first() is None


def test_new_account_cannot_grant_cross_organization_companies(client, firm):
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    with firm.db.write() as session:
        create_organization(session, "firm-outside", "Firm Outside", "Out Owner")
        RecordRepository(session, "firm-outside").insert("outside-co", "company", {"name": "Outside Co"})
    created = owner.v2({"action": "member", "name": "Scoped", "role": "Accountant", "companies": ["c1", "outside-co"],
                        "email": "scoped@firm-a.test", "password": "a starter pass phrase", "confirmPassword": "a starter pass phrase"})
    profile = next(m for m in owner.state()["members"] if m["id"] == created["id"])
    assert profile["companies"] == ["c1"]  # the foreign company id was silently dropped, not granted


def test_forced_change_gate_still_works_if_the_flag_is_ever_set(client, firm):
    """No code path sets ``must_change_password`` any more (removed by design), but the
    enforcement in ``load_context`` is still real infrastructure and must keep blocking non-exempt
    endpoints correctly if the flag is ever set directly (e.g. a future admin-forced reset)."""
    owner = login(client, "owner@firm-a.test", OWNER_PASSWORD)
    owner.v2({"action": "member", "name": "Nora New", "role": "Accountant", "companies": ["c1"],
             "email": "nora@firm-a.test", "password": "a starter pass phrase", "confirmPassword": "a starter pass phrase"})
    with firm.db.write() as session:
        session.execute(update(User).where(User.email == "nora@firm-a.test").values(must_change_password=True))
    nora = login(client, "nora@firm-a.test", "a starter pass phrase")
    assert nora.call("/api/auth/me")["mustChangePassword"] is True
    # Every ordinary business endpoint is refused while the change is pending — including
    # team-administration endpoints (which need Owner anyway; the pending reason comes first).
    for method, path in (("GET", "/api/v2"), ("GET", "/api/portal"), ("GET", "/api/v2/records"), ("GET", "/api/auth/team"), ("GET", "/api/auth/sessions")):
        response = nora.request(method, path)
        assert response.status_code == 403 and "change" in response.json()["error"].lower()
    # The exempted endpoints still work.
    assert nora.call("/api/auth/me")["user"]["email"] == "nora@firm-a.test"
    nora.call("/api/auth/password/change", {"current": "a starter pass phrase", "password": "a completely different phrase"})
    # The flag is cleared: ordinary endpoints work now, and /me no longer reports it pending.
    assert nora.call("/api/auth/me")["mustChangePassword"] is False
    assert nora.request("GET", "/api/v2").status_code == 200
