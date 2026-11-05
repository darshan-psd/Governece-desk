"""Team Members in the client portal: accounts, roles, company isolation and username sign-in.

Every test runs on its own throwaway database (SQLite by default; ``GOVERNANCE_TEST_DATABASE=postgres``
runs them on a temporary PostgreSQL cluster) with synthetic firms, companies and people only.
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from app.api.deps import services_of
from app.auth import Principal
from app.core.config import Settings
from app.core.errors import ApiError
from app.database.migrations import upgrade_to_head
from app.main import create_app
from app.models import CompanyAccess, Membership, SecurityEvent, User
from app.repositories.identity import IdentityRepository
from app.repositories.records import RecordRepository
from app.services import team_service
from app.services.context import load_context
from app.services.identity_service import add_member, create_organization, identity_ids, set_credentials
from tests.conftest import Api

SESSION = "gov_session"
PASSWORD = "correct horse battery staple"
MEMBER_PASSWORD = "river stone lantern 42"
NEW_PASSWORD = "quiet meadow compass 77"
PROFILE = {"name": "Company One", "registration": "U72900TN2026PTC123456", "pan": "ABCDE1234F",
           "directors": [{"name": "Synthetic Director"}]}


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
    """One firm with staff, and two client companies whose primary contacts sign in by email."""
    services = services_of(client)
    with services.db.write() as session:
        _org, owner = create_organization(session, "firm-a", "Firm A", "Asha Owner")
        records = RecordRepository(session, "firm-a")
        records.insert("c1", "company", PROFILE)
        records.insert("c2", "company", {"name": "Company Two"})
        set_credentials(session, owner.id, "owner@firm-a.test", PASSWORD)
        for member_id, name, role, companies in (
            ("sec", "Sam Secretary", "Secretary", ["*"]), ("sec2", "Sia Secretary", "Secretary", ["c2"]),
            ("acc", "Ann Accountant", "Accountant", ["*"]), ("dir", "Dee Director", "Director", ["*"]),
            ("aud", "Al Auditor", "Auditor", ["*"]), ("cli", "Cleo Client", "Client", ["c1"]), ("cli2", "Dan Client", "Client", ["c2"]),
        ):
            records.insert(member_id, "member", {"name": name, "role": role, "companies": companies, "active": True})
            membership = add_member(session, "firm-a", member_id, name, role, companies)
            set_credentials(session, membership.id, f"{member_id}@firm-a.test", PASSWORD)
    return services


def signin(client, identifier: str, password: str = PASSWORD, expect: int = 200) -> Api:
    api = Api(client, cookie_name=SESSION)
    response = api.request("POST", "/api/auth/login", json={"identifier": identifier, "password": password})
    assert response.status_code == expect, response.text
    if expect == 200:
        api.cookie = response.cookies.get(SESSION)
        assert api.cookie
    return api


def team(api: Api, company: str = "c1", expect: int = 200) -> dict:
    return api.call(f"/api/portal/team?company={company}", expect=expect)


def act(api: Api, action: str, expect: int = 200, **body) -> dict:
    return api.call(f"/api/portal/team/{action}", {"company": "c1", **body}, expect)


def add(api: Api, username: str = "ravi.k", role: str = "Employee", expect: int = 200, **overrides) -> dict:
    body = {"name": "Ravi Kumar", "username": username, "password": MEMBER_PASSWORD, "confirmPassword": MEMBER_PASSWORD,
            "role": role, **overrides}
    return act(api, "create", expect, **body)


def member_of(api: Api, member: str, company: str = "c1") -> dict:
    return next(m for m in team(api, company)["members"] if m["member"] == member)


def audit(firm, owner: Api) -> list[str]:
    return [a["action"] for a in owner.state()["audit"]]


def events(firm, name: str) -> list[SecurityEvent]:
    with firm.db.read() as session:
        return list(session.scalars(select(SecurityEvent).where(SecurityEvent.event == name)))


def user_of(firm, member: str) -> User:
    with firm.db.read() as session:
        return session.get(User, identity_ids("firm-a", member)[0])


@pytest.fixture
def owner(client, firm) -> Api:
    return signin(client, "owner@firm-a.test")


@pytest.fixture
def admin(client, firm) -> Api:
    """Cleo: Company One's primary client contact, so its Company Admin."""
    return signin(client, "cli@firm-a.test")


@pytest.fixture
def desk(firm, owner) -> dict:
    """One item for each portal module of Company One, and a shared document of Company Two."""
    documents = {
        "financial": owner.upload("c1", filename="accounts.txt", content=b"Accounts", category="Financial statements", shared="true"),
        "legal": owner.upload("c1", filename="moa.txt", content=b"Articles", category="MOA & AOA", shared="true"),
        "internal": owner.upload("c1", filename="notes.txt", content=b"Internal", category="Other", shared="false"),
        "foreign": owner.upload("c2", filename="other.txt", content=b"Other company", category="Financial statements", shared="true"),
    }
    owner.portal({"action": "obligationCreate", "company": "c1", "data": {"title": "Board filing", "due": "2030-01-15", "source": "Synthetic"}})
    owner.portal({"action": "startOnboarding", "company": "c1"})
    owner.portal({"action": "threadCreate", "company": "c1", "subject": "Synthetic thread", "content": "Hello"})
    with firm.db.write() as session:
        RecordRepository(session, "firm-a").insert("inv1", "invoice", {
            "company": "c1", "status": "Issued", "number": "INV-1", "title": "INV-1", "total": 10000, "due": "2030-01-15", "items": []})
    return documents


# Creating accounts ------------------------------------------------------------------------------

def test_a_company_admin_creates_a_working_account_directly(client, firm, owner, admin):
    created = add(admin, role="Director", designation="Finance head")
    assert created["ok"] is True and "password" not in json.dumps(created).lower()
    listed = member_of(admin, created["member"])
    assert listed["name"] == "Ravi Kumar" and listed["username"] == "ravi.k" and listed["role"] == "Director"
    assert listed["designation"] == "Finance head" and listed["status"] == "Active" and listed["lastActive"] is None
    assert listed["company"] == {"id": "c1", "name": "Company One"} and listed["email"] is None

    # A real account: no email, active, only ever a Client to the firm, limited to this company.
    user = user_of(firm, created["member"])
    assert user.email is None and user.username == "ravi.k" and user.status == "active" and user.must_change_password is False
    assert user.password_hash.startswith("$argon2id$") and MEMBER_PASSWORD not in user.password_hash
    with firm.db.read() as session:
        membership = session.get(Membership, identity_ids("firm-a", created["member"])[1])
        assert membership.role == "Client" and membership.status == "active" and membership.all_companies == 0
        access = list(session.scalars(select(CompanyAccess).where(CompanyAccess.membership_id == membership.id)))
    assert [(a.company_id, a.client_role) for a in access] == [("c1", "Director")]

    member = signin(client, "ravi.k", MEMBER_PASSWORD)
    me = member.call("/api/auth/me")
    assert me["user"]["username"] == "ravi.k" and me["user"]["email"] is None and me["membership"]["role"] == "Client"
    assert member_of(admin, created["member"])["lastActive"] is not None

    assert any(a.startswith("Created team member (Director)") for a in audit(firm, owner))
    assert len(events(firm, "team_member_created")) == 1
    assert not any("mail-outbox" in str(p) for p in firm.settings.governance_data_dir.iterdir())  # nothing was emailed


def test_the_list_counts_people_and_never_exposes_credentials(admin):
    member = add(admin)["member"]
    add(admin, username="meena", name="Meena Rao", role="Accountant")
    act(admin, "status", member=member, status="inactive")
    listing = team(admin)
    assert listing["summary"] == {"total": 3, "active": 2, "deactivated": 1}
    assert [m["name"] for m in listing["members"]] == ["Cleo Client", "Meena Rao", "Ravi Kumar"]
    assert next(m for m in listing["members"] if m["self"])["role"] == "Company Admin"
    dump = json.dumps(listing).lower()
    assert "password" not in dump and "hash" not in dump and "argon2" not in dump


@pytest.mark.parametrize("change, message", [
    ({"name": ""}, "full name"),
    ({"username": "ab"}, "3 to 32"), ({"username": "has space"}, "3 to 32"), ({"username": "ravi@firm.test"}, "3 to 32"),
    ({"username": "-ravi"}, "3 to 32"), ({"username": "r" * 33}, "3 to 32"), ({"username": None}, "3 to 32"),
    ({"confirmPassword": "something else entirely"}, "do not match"),
    ({"password": "short", "confirmPassword": "short"}, "12 to 256"),
    ({"password": "password1234", "confirmPassword": "password1234"}, "too easy"),
    ({"password": "ravi.k-river-stone", "confirmPassword": "ravi.k-river-stone"}, "must not contain"),
    ({"role": "Secretary"}, "valid role"), ({"role": "Owner"}, "valid role"), ({"role": "Client"}, "valid role"), ({"role": ""}, "valid role"),
    ({"role": "Employee", "modules": ["team"]}, "valid modules"),
])
def test_creation_is_validated_and_leaves_nothing_behind(firm, admin, change, message):
    before = team(admin)["summary"]["total"]
    assert message in add(admin, expect=400, **change)["error"]
    assert team(admin)["summary"]["total"] == before
    with firm.db.read() as session:
        assert session.scalars(select(User).where(User.display_name == "Ravi Kumar")).first() is None


def test_usernames_are_unique_whatever_the_case_or_company(client, firm, admin):
    add(admin)
    assert "already taken" in add(admin, username="RAVI.K", expect=409)["error"]
    other = signin(client, "cli2@firm-a.test")
    assert "already taken" in act(other, "create", 409, company="c2", name="Other Ravi", username="ravi.k", password=MEMBER_PASSWORD,
                                  confirmPassword=MEMBER_PASSWORD, role="Employee")["error"]
    with firm.db.read() as session:
        assert session.scalars(select(User).where(User.display_name == "Other Ravi")).first() is None
    with pytest.raises(IntegrityError), firm.db.write() as session:  # the database refuses it too, whatever the code does
        session.execute(text("UPDATE users SET username = 'RAVI.K' WHERE id = :id"), {"id": identity_ids("firm-a", "cli")[0]})


# Signing in -------------------------------------------------------------------------------------

def test_username_sign_in_and_email_sign_in_work_side_by_side(client, firm, admin):
    add(admin)
    signin(client, "  Ravi.K ", MEMBER_PASSWORD)  # case and spaces are forgiven, as for emails
    legacy = Api(client, cookie_name=SESSION).request("POST", "/api/auth/login", json={"email": "cli@firm-a.test", "password": PASSWORD})
    assert legacy.status_code == 200  # existing clients and API callers keep using ``email``
    for identifier, password in (("ravi.k", "wrong password entirely"), ("nobody.here", MEMBER_PASSWORD), ("ra", MEMBER_PASSWORD), ("", MEMBER_PASSWORD)):
        failed = signin(client, identifier, password, expect=401)
        assert failed.cookie is None
    response = Api(client, cookie_name=SESSION).request("POST", "/api/auth/login", json={"identifier": "ravi.k", "password": "wrong"})
    assert response.json() == {"error": "The username, email or password is incorrect."}


def test_username_sign_in_is_throttled_like_email(client, firm, admin):
    add(admin)
    for _ in range(firm.settings.governance_login_max_failures):
        signin(client, "ravi.k", "wrong password entirely", expect=401)
    signin(client, "ravi.k", MEMBER_PASSWORD, expect=429)


# Managing people --------------------------------------------------------------------------------

def test_edit_details_and_username(client, firm, owner, admin):
    member = add(admin)["member"]
    act(admin, "update", member=member, name="Ravi K Sharma", designation="Compliance lead", username="ravi.sharma")
    listed = member_of(admin, member)
    assert (listed["name"], listed["designation"], listed["username"]) == ("Ravi K Sharma", "Compliance lead", "ravi.sharma")
    assert user_of(firm, member).display_name == "Ravi K Sharma"
    signin(client, "ravi.k", MEMBER_PASSWORD, expect=401)
    signin(client, "ravi.sharma", MEMBER_PASSWORD)
    add(admin, username="taken.name", name="Someone Else")
    assert "already taken" in act(admin, "update", 409, member=member, username="TAKEN.NAME")["error"]
    assert "full name" in act(admin, "update", 400, member=member, name=" ")["error"]
    assert any(a.startswith("Updated team member details") for a in audit(firm, owner))


def test_changing_a_role_applies_to_the_next_request(client, firm, owner, admin, desk):
    member = add(admin, role="Director")["member"]
    session = signin(client, "ravi.k", MEMBER_PASSWORD)
    assert {d["id"] for d in session.portal()["documents"]} == {desk["financial"], desk["legal"]}
    act(admin, "update", member=member, role="Employee", modules=["inbox", "bogus"], expect=400)
    act(admin, "update", member=member, role="Employee", modules=["inbox"])
    state = session.portal()
    assert state["documents"] == [] and len(state["threads"]) == 1 and state["access"]["c1"]["role"] == "Employee"
    assert member_of(admin, member)["modules"] == ["inbox"]
    assert any(a.startswith("Changed team member role to Employee") for a in audit(firm, owner))
    assert json.loads(events(firm, "team_member_role_changed")[0].detail)["previous_role"] == "Director"


def test_password_reset_replaces_the_password_and_ends_sessions(client, firm, owner, admin):
    member = add(admin)["member"]
    session = signin(client, "ravi.k", MEMBER_PASSWORD)
    before = user_of(firm, member).password_hash
    assert "do not match" in act(admin, "password", 400, member=member, password=NEW_PASSWORD, confirmPassword="different one here")["error"]
    assert "12 to 256" in act(admin, "password", 400, member=member, password="short", confirmPassword="short")["error"]
    act(admin, "password", member=member, password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)
    after = user_of(firm, member).password_hash
    assert after != before and after.startswith("$argon2id$") and NEW_PASSWORD not in after
    assert session.request("GET", "/api/portal").status_code == 401
    signin(client, "ravi.k", MEMBER_PASSWORD, expect=401)
    signin(client, "ravi.k", NEW_PASSWORD)
    assert any(a.startswith("Reset team member password") for a in audit(firm, owner))
    assert not any(NEW_PASSWORD in (e.detail or "") for e in events(firm, "team_member_password_reset"))


def test_a_reset_unlocks_a_throttled_account(client, firm, admin):
    member = add(admin)["member"]
    for _ in range(firm.settings.governance_login_max_failures):
        signin(client, "ravi.k", "wrong password entirely", expect=401)
    act(admin, "password", member=member, password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)
    signin(client, "ravi.k", NEW_PASSWORD)


def test_deactivation_blocks_sign_in_at_once_and_reactivation_restores_it(client, firm, owner, admin):
    member = add(admin)["member"]
    session = signin(client, "ravi.k", MEMBER_PASSWORD)
    act(admin, "status", member=member, status="inactive")
    assert session.request("GET", "/api/portal").status_code == 401
    signin(client, "ravi.k", MEMBER_PASSWORD, expect=403)
    assert member_of(admin, member)["status"] == "Deactivated"
    act(admin, "status", 400, member=member, status="inactive")
    act(admin, "status", 400, member=member, status="archived")
    act(admin, "status", member=member, status="active")
    signin(client, "ravi.k", MEMBER_PASSWORD)
    log = audit(firm, owner)
    assert any(a.startswith("Deactivated team member") for a in log) and any(a.startswith("Reactivated team member") for a in log)


def test_removal_ends_the_account_and_keeps_history(client, firm, owner, admin):
    member = add(admin)["member"]
    session = signin(client, "ravi.k", MEMBER_PASSWORD)
    owner.create("request", {"company": "c1", "title": "Bank statement", "due": "2030-01-15", "assignee": member})
    assert "1 open request" in act(admin, "remove", member=member)["warning"]
    assert session.request("GET", "/api/portal").status_code == 401
    signin(client, "ravi.k", MEMBER_PASSWORD, expect=401)
    assert [m["member"] for m in team(admin)["members"]] == ["cli"]
    user = user_of(firm, member)
    assert user.status == "inactive" and user.password_hash is None and user.username is None
    profile = next(m for m in owner.state()["members"] if m["id"] == member)  # past work still names them
    assert profile["name"] == "Ravi Kumar" and profile["active"] is False and profile["companies"] == []
    act(admin, "remove", 404, member=member)
    add(admin)  # the username is free again, as a new account
    assert any(a.startswith("Removed team member") for a in audit(firm, owner))


def test_a_company_always_keeps_an_active_admin(client, firm):
    owner = signin(client, "owner@firm-a.test")
    # Cleo is the only admin: nobody can take that away, not even the firm from this page.
    assert "at least one active Company Admin" in act(owner, "status", 400, member="cli", status="inactive")["error"]
    assert "at least one active Company Admin" in act(owner, "remove", 400, member="cli")["error"]
    assert "at least one active Company Admin" in act(owner, "update", 400, member="cli", role="Director")["error"]
    cleo = signin(client, "cli@firm-a.test")
    assert member_of(cleo, "cli")["lastAdmin"] is True
    second = add(cleo, role="Company Admin")["member"]
    assert member_of(cleo, "cli")["lastAdmin"] is False
    # With a second admin the first can step down, and then the second is the last one.
    act(owner, "update", member="cli", role="Employee")
    assert "at least one active Company Admin" in act(owner, "status", 400, member=second, status="inactive")["error"]
    act(owner, "update", member="cli", role="Company Admin")
    act(owner, "status", member=second, status="inactive")
    # A deactivated admin does not count, so Cleo is the last active one again.
    assert "at least one active Company Admin" in act(owner, "remove", 400, member="cli")["error"]


def test_nobody_changes_their_own_access_here(admin):
    assert "own access" in act(admin, "status", 400, member="cli", status="inactive")["error"]
    assert "remove yourself" in act(admin, "remove", 400, member="cli")["error"]
    assert "own role" in act(admin, "update", 400, member="cli", role="Director")["error"]
    assert "own password" in act(admin, "password", 400, member="cli", password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)["error"]
    act(admin, "update", member="cli", name="Cleo Client", designation="Founder", role="Company Admin")  # own details are fine
    assert member_of(admin, "cli")["designation"] == "Founder"


def test_two_admins_cannot_remove_each_other_at_the_same_moment(client, firm):
    cleo = signin(client, "cli@firm-a.test")
    second = add(cleo, role="Company Admin")["member"]
    contexts = []
    for member in ("cli", second):
        with firm.db.read() as session:
            membership_id = session.get(Membership, identity_ids("firm-a", member)[1]).id
        contexts.append(load_context(firm, Principal(membership_id)))
    barrier, outcomes = threading.Barrier(2), []

    def deactivate(ctx, target):
        ctx.store = type(ctx.store)(firm.db, ctx.owner)  # one store per thread
        barrier.wait()
        try:
            team_service.team_status(firm, ctx, {"company": "c1", "member": target, "status": "inactive"}, "127.0.0.1")
            outcomes.append("done")
        except ApiError as error:
            outcomes.append(error.message)

    threads = [threading.Thread(target=deactivate, args=(contexts[0], second)), threading.Thread(target=deactivate, args=(contexts[1], "cli"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert outcomes.count("done") == 1 and len(outcomes) == 2, outcomes
    with firm.db.read() as session:
        assert len(team_service._active_admins(session, "firm-a", "c1")) == 1


def test_the_last_admin_check_waits_for_a_change_in_progress(client, firm):
    """Deterministic version of the race above: one change is held open, mid-transaction, while the
    other runs. It must wait, then count afresh, and be refused."""
    cleo = signin(client, "cli@firm-a.test")
    second = add(cleo, role="Company Admin")["member"]
    second_membership = identity_ids("firm-a", second)[1]
    ctx = load_context(firm, Principal(second_membership))
    outcome = []

    def deactivate_cleo():
        try:
            team_service.team_status(firm, ctx, {"company": "c1", "member": "cli", "status": "inactive"}, "127.0.0.1")
            outcome.append("done")
        except ApiError as error:
            outcome.append(error.message)

    with firm.db.write() as session:  # Cleo deactivating the second admin, not yet committed
        team_service._keep_an_admin(session, "firm-a", second_membership, ["c1"])
        IdentityRepository(session).set_membership_status(second_membership, "inactive")
        thread = threading.Thread(target=deactivate_cleo)
        thread.start()
        thread.join(1.5)
        assert thread.is_alive() and outcome == []  # waiting for the lock, not acting on old data
    thread.join(30)
    assert len(outcome) == 1 and "at least one active Company Admin" in outcome[0]
    with firm.db.read() as session:
        assert team_service._active_admins(session, "firm-a", "c1") == {identity_ids("firm-a", "cli")[1]}


# Who may manage ---------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["Director", "Accountant", "Employee"])
def test_team_members_cannot_manage_the_team(client, firm, admin, role):
    other = add(admin, username="target.person", name="Target Person")["member"]
    add(admin, role=role)
    member = signin(client, "ravi.k", MEMBER_PASSWORD)
    assert "team" not in member.portal()["access"]["c1"]["permissions"]
    team(member, expect=403)
    add(member, username="sneaky.one", role="Company Admin", expect=403)
    act(member, "update", 403, member=other, role="Company Admin")
    act(member, "password", 403, member=other, password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)
    act(member, "status", 403, member=other, status="inactive")
    act(member, "remove", 403, member=other)
    signin(client, "target.person", MEMBER_PASSWORD)


@pytest.mark.parametrize("email", ["acc@firm-a.test", "dir@firm-a.test", "aud@firm-a.test"])
def test_other_firm_staff_do_not_manage_client_teams(client, firm, email):
    staff = signin(client, email)
    team(staff, expect=403)
    add(staff, expect=403)


def test_the_firm_can_help_a_company_it_has_access_to(client, firm, owner):
    created = add(owner, role="Company Admin")["member"]
    assert member_of(owner, created)["role"] == "Company Admin"
    secretary = signin(client, "sec@firm-a.test")
    act(secretary, "password", member=created, password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)  # a locked-out admin gets back in
    signin(client, "ravi.k", NEW_PASSWORD)
    scoped = signin(client, "sec2@firm-a.test")  # a secretary of Company Two only
    team(scoped, expect=404)
    add(scoped, username="not.allowed", expect=404)
    assert team(scoped, "c2")["summary"]["total"] == 1


def test_anonymous_and_forged_requests_are_refused(client, firm, admin):
    anonymous = Api(client, cookie_name=SESSION)
    assert anonymous.request("GET", "/api/portal/team?company=c1").status_code == 401
    assert anonymous.request("POST", "/api/portal/team/create", json={"company": "c1"}).status_code == 401
    assert admin.request("POST", "/api/portal/team/create", json={"company": "c1"}, headers={"x-csrf-token": "forged"}).status_code == 403
    assert admin.request("POST", "/api/portal/team/unknown", json={"company": "c1"}).status_code == 404


# Company and organization isolation -------------------------------------------------------------

def test_an_admin_cannot_reach_another_company(client, firm, admin):
    other = signin(client, "cli2@firm-a.test")
    outsider = act(other, "create", company="c2", name="Other Person", username="other.person", password=MEMBER_PASSWORD,
                   confirmPassword=MEMBER_PASSWORD, role="Employee")["member"]
    team(admin, "c2", expect=404)
    add(admin, company="c2", username="planted.one", expect=404)
    act(admin, "status", 404, company="c2", member=outsider, status="inactive")
    # Naming their own company with someone else's member changes nothing either.
    for action, body in (("update", {"name": "Hacked"}), ("status", {"status": "inactive"}), ("remove", {}),
                         ("password", {"password": NEW_PASSWORD, "confirmPassword": NEW_PASSWORD})):
        act(admin, action, 404, member=outsider, **body)
    signin(client, "other.person", MEMBER_PASSWORD)
    assert [m["member"] for m in team(admin)["members"]] == ["cli"]


def test_a_member_of_company_a_gets_nothing_of_company_b(client, firm, admin, desk):
    add(admin, role="Director")
    member = signin(client, "ravi.k", MEMBER_PASSWORD)
    state = member.portal()
    assert [c["id"] for c in state["companies"]] == ["c1"]
    assert all(d["company"] == "c1" for d in state["documents"])
    assert member.request("GET", f"/api/v2-files?id={desk['foreign']}").status_code == 404
    assert member.request("GET", "/api/portal?export=calendar&company=c2").status_code == 404


def test_staff_cannot_be_reached_through_a_client_team(client, firm, owner, admin):
    for target in ("owner", "sec", "dir"):
        act(admin, "password", 404, member=target, password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)
        act(admin, "status", 404, member=target, status="inactive")
    act(owner, "status", 404, member="sec", status="inactive")  # this page only ever acts on client users


def test_another_organization_is_invisible(client, firm, admin):
    with firm.db.write() as session:
        create_organization(session, "firm-b", "Firm B", "Bala Owner")
        records = RecordRepository(session, "firm-b")
        records.insert("b1", "company", {"name": "Firm B Company"})
        records.insert("bcli", "member", {"name": "Bina Client", "role": "Client", "companies": ["b1"], "active": True})
        membership = add_member(session, "firm-b", "bcli", "Bina Client", "Client", ["b1"])
        set_credentials(session, membership.id, "bcli@firm-b.test", PASSWORD)
    outsider = signin(client, "bcli@firm-b.test")
    team(outsider, "c1", expect=404)
    add(outsider, company="c1", username="planted.two", expect=404)
    team(admin, "b1", expect=404)
    act(admin, "status", 404, company="b1", member="bcli", status="inactive")
    assert team(outsider, "b1")["summary"]["total"] == 1  # their own team is unaffected
    add(outsider, company="b1", username="ravi.k")
    assert "already taken" in add(admin, username="ravi.k", expect=409)["error"]  # unique across firms: sign-in has no firm field


def test_a_person_in_two_companies_is_protected_from_one_companys_admin(client, firm, owner, admin):
    member = add(admin, role="Director")["member"]
    owner.call("/api/auth/team/companies", {"member": member, "companies": ["c1", "c2"]})
    with firm.db.read() as session:  # being added to another company gives no rights there
        access = {a.company_id: a.client_role for a in session.scalars(select(CompanyAccess).where(
            CompanyAccess.membership_id == identity_ids("firm-a", member)[1]))}
    assert access == {"c1": "Director", "c2": "Employee"}

    assert member_of(admin, member)["accountManaged"] is False
    for action, body in (("password", {"password": NEW_PASSWORD, "confirmPassword": NEW_PASSWORD}), ("status", {"status": "inactive"}),
                         ("update", {"name": "Renamed"}), ("update", {"username": "renamed.one"})):
        assert "another company" in act(admin, action, 403, member=member, **body)["error"]
    signin(client, "ravi.k", MEMBER_PASSWORD)  # nothing changed

    act(admin, "update", member=member, role="Accountant")  # their role here is this company's business
    assert member_of(admin, member)["role"] == "Accountant"
    act(admin, "remove", member=member)  # and so is removing them from it
    session = signin(client, "ravi.k", MEMBER_PASSWORD)
    assert [c["id"] for c in session.portal()["companies"]] == ["c2"]
    assert user_of(firm, member).status == "active"


# What each role can see and do -------------------------------------------------------------------

ROLES = {
    "Company Admin": dict(modules=None, documents={"financial", "legal"}, invoices=1, obligations=1, cases=1, threads=1, profile=True),
    "Director": dict(modules=None, documents={"financial", "legal"}, invoices=0, obligations=1, cases=1, threads=0, profile=True),
    "Accountant": dict(modules=None, documents={"financial"}, invoices=1, obligations=0, cases=0, threads=0, profile=False),
    "Employee": dict(modules=[], documents=set(), invoices=0, obligations=0, cases=0, threads=0, profile=False),
    "Employee with modules": dict(modules=["documents", "inbox"], documents={"financial", "legal"}, invoices=0, obligations=0, cases=0, threads=1, profile=False),
}


@pytest.mark.parametrize("label", ROLES)
def test_each_role_receives_only_its_modules_everywhere(client, firm, admin, desk, label):
    expected = ROLES[label]
    add(admin, role=label.split(" with ")[0], modules=expected["modules"])
    member = signin(client, "ravi.k", MEMBER_PASSWORD)
    state = member.portal()
    assert {d["id"] for d in state["documents"]} == {desk[k] for k in expected["documents"]}
    assert (len(state["invoices"]), len(state["obligations"]), len(state["cases"]), len(state["threads"])) == (
        expected["invoices"], expected["obligations"], expected["cases"], expected["threads"])
    assert ("pan" in state["companies"][0]) is expected["profile"]
    assert state["companies"][0]["name"] == "Company One"
    if not expected["obligations"]:
        assert state["calendar"] == [] or all(c["type"] == "Payment" for c in state["calendar"])

    # The same limits hold on every other route to the data, not just the portal page.
    workspace = member.state()
    assert {r["kind"] for r in workspace["records"]} <= {"company", "request", "document"}
    assert {r["id"] for r in workspace["records"] if r["kind"] == "document"} == {desk[k] for k in expected["documents"]}
    assert ("pan" in next(r for r in workspace["records"] if r["kind"] == "company")) is expected["profile"]
    listed = member.call("/api/v2/records?kind=document&kind=company")
    assert {r["id"] for r in listed["items"] if r["kind"] == "document"} == {desk[k] for k in expected["documents"]}
    assert ("pan" in next(r for r in listed["items"] if r["kind"] == "company")) is expected["profile"]
    for key, document in desk.items():
        allowed = key in expected["documents"]
        assert member.request("GET", f"/api/v2-files?id={document}").status_code == (200 if allowed else 404), key
        assert member.request("GET", f"/api/v2-files/versions?id={document}").status_code == (200 if allowed else 404), key
    bundle = member.request("GET", "/api/portal?export=bundle&company=c1")
    assert bundle.status_code == (200 if expected["documents"] else 400)
    assert member.request("GET", "/api/portal?export=calendar&company=c1").status_code == (200 if expected["obligations"] else 403)
    assert member.request("GET", "/api/portal?export=invoice&company=c1&id=inv1").status_code == (200 if expected["invoices"] else 404)
    # Internal work stays out of reach whatever the job title says.
    member.v2({"action": "create", "kind": "task", "data": {"company": "c1", "title": "x", "due": "2030-01-01"}}, 403)
    assert member.request("GET", "/api/v2?export=csv").status_code == 403
    assert member.request("GET", "/api/v2/audit").status_code == 403
    assert member.request("GET", "/api/auth/team").status_code == 403


def company_save(api: Api, expect: int) -> None:
    company = next(c for c in api.portal()["companies"] if c["id"] == "c1")
    api.portal({"action": "companySave", "id": "c1", "revision": company["revision"], "data": PROFILE}, expect)


@pytest.mark.parametrize("role, modules, can_edit, can_upload, can_message, can_pay", [
    ("Company Admin", None, True, True, True, True),
    ("Director", None, False, False, False, False),
    ("Accountant", None, False, True, False, True),
    ("Employee", [], False, False, False, False),
    ("Employee", ["inbox"], False, False, True, False),
    ("Employee", ["invoices"], False, True, False, True),
])
def test_actions_are_refused_on_the_server(client, firm, admin, desk, role, modules, can_edit, can_upload, can_message, can_pay):
    add(admin, role=role, modules=modules)
    member = signin(client, "ravi.k", MEMBER_PASSWORD)
    company_save(member, 200 if can_edit else 403)
    uploaded = member.upload("c1", filename="proof.txt", content=b"Proof", category="Invoices & payment proof", expect=200 if can_upload else 403)
    member.portal({"action": "threadCreate", "company": "c1", "subject": "Hi", "content": "Hello"}, 200 if can_message else 403)
    invoice = next((i for i in member.portal()["invoices"]), {"id": "inv1", "revision": 1})
    response = member.request("POST", "/api/portal", json={
        "action": "paymentReport", "company": "c1", "id": invoice["id"], "revision": invoice["revision"], "amount": "10.00",
        "reference": "SYNTHETIC-1", "paidAt": "2026-01-01", "evidence": [uploaded] if uploaded else []})
    assert response.status_code == (200 if can_pay else 403), response.text
    if can_upload:  # your own upload stays readable, whatever your document rights
        assert member.request("GET", f"/api/v2-files?id={uploaded}").status_code == 200


def test_assigned_requests_work_for_every_role_and_only_for_that_person(client, firm, owner, admin, desk):
    member = add(admin, role="Employee", modules=[])["member"]
    add(admin, username="peer.person", name="Peer Person", role="Employee", modules=[])
    request = owner.create("request", {"company": "c1", "title": "Upload the lease", "due": "2030-01-15", "assignee": member})
    linked = owner.upload("c1", filename="template.txt", content=b"Template", category="Other", shared="true")
    with firm.db.write() as session:
        repo = RecordRepository(session, "firm-a")
        stored = repo.get(request)
        repo.update_if_revision(request, stored["revision"], {**stored, "evidence": [linked]})

    session = signin(client, "ravi.k", MEMBER_PASSWORD)
    state = session.portal()
    assert [r["id"] for r in state["requests"]] == [request]
    assert [w["id"] for w in state["work"]] == [request]  # the Requests page reads these rows
    assert {d["id"] for d in state["documents"]} == {linked}  # sent for their request, nothing else
    assert session.request("GET", f"/api/v2-files?id={linked}").status_code == 200
    answer = session.upload("c1", filename="lease.csv", content=b"rent,term\n10,12", category="Other", request=request)
    revision = next(d["revision"] for d in session.portal()["documents"] if d["id"] == answer)
    edited = session.request("POST", "/api/v2-files/revision", data={"id": answer, "revision": str(revision)},
                             files={"file": ("lease.csv", b"rent,term\n11,12")})
    assert edited.status_code == 200, edited.text
    # An edited version is not listed on the request, only linked to it, and stays readable.
    assert session.request("GET", f"/api/v2-files?id={edited.json()['id']}").status_code == 200
    current = session.get(request)
    session.v2({"action": "respond", "id": request, "revision": current["revision"], "response": "Attached."})

    peer = signin(client, "peer.person", MEMBER_PASSWORD)
    assert peer.portal()["requests"] == [] and peer.portal()["documents"] == []
    assert peer.request("GET", f"/api/v2-files?id={answer}").status_code == 404
    peer.upload("c1", filename="x.txt", content=b"x", category="Other", request=request, expect=404)


def test_only_a_company_admin_adds_a_company(client, firm, admin):
    add(admin, role="Director")
    member = signin(client, "ravi.k", MEMBER_PASSWORD)
    member.portal({"action": "companyCreate", "data": {"name": "Unauthorised Company"}}, 403)
    created = admin.portal({"action": "companyCreate", "data": {"name": "Cleo's Second Company"}})["id"]
    assert team(admin, created)["summary"]["total"] == 1  # they run the company they proposed


def test_existing_accounts_are_unaffected(client, firm, owner, admin, desk):
    """A client who existed before Team Members keeps all of it, and is simply its Company Admin."""
    state = admin.portal()
    assert state["access"]["c1"]["role"] == "Company Admin" and "team" in state["access"]["c1"]["permissions"]
    assert {d["id"] for d in state["documents"]} == {desk["financial"], desk["legal"]}
    assert len(state["invoices"]) == len(state["obligations"]) == len(state["threads"]) == 1 and "pan" in state["companies"][0]
    company_save(admin, 200)
    with firm.db.read() as session:  # no role was written for them
        assert session.get(CompanyAccess, (identity_ids("firm-a", "cli")[1], "c1")).client_role is None
    staff = owner.portal()
    assert staff["access"]["c1"]["role"] is None and "team" in staff["access"]["c1"]["permissions"]
    assert signin(client, "acc@firm-a.test").portal()["access"]["c1"]["permissions"].count("team") == 0
    assert next(m for m in owner.state()["members"] if m["id"] == "cli")["role"] == "Client"


def test_a_team_member_can_receive_requests_from_the_firm(firm, owner, admin):
    member = add(admin, role="Accountant")["member"]
    listed = next(m for m in owner.state()["members"] if m["id"] == member)
    assert listed["role"] == "Client" and listed["companies"] == ["c1"] and listed["client"] is True
    owner.create("request", {"company": "c1", "title": "Bank statement", "due": "2030-01-15", "assignee": member})


def test_audit_and_events_stay_with_the_firm_and_hold_no_secrets(client, firm, owner, admin):
    member = add(admin)["member"]
    act(admin, "password", member=member, password=NEW_PASSWORD, confirmPassword=NEW_PASSWORD)
    assert admin.request("GET", "/api/v2/audit").status_code == 403
    assert admin.request("GET", "/api/auth/security-events").status_code == 403
    assert admin.state()["audit"] == []
    log = owner.call("/api/auth/security-events")
    dump = json.dumps([log, owner.state()["audit"]])
    assert MEMBER_PASSWORD not in dump and NEW_PASSWORD not in dump and "argon2" not in dump


# Local preview ----------------------------------------------------------------------------------

def test_team_members_work_in_local_preview_too(settings):
    with TestClient(create_app(settings), base_url="http://testserver") as test_client:
        api = Api(test_client).sign_in()
        api.v2({"action": "initialise"})
        company = api.portal({"action": "companyCreate", "clientName": "Preview Client", "data": {"name": "Preview Company"}})["id"]
        created = act(api, "create", company=company, name="Preview Member", username="preview.member", password=MEMBER_PASSWORD,
                      confirmPassword=MEMBER_PASSWORD, role="Employee", modules=["inbox"])["member"]
        api.role(created)  # "Preview as"
        state = api.portal()
        assert state["access"][company]["role"] == "Employee" and state["access"][company]["permissions"] == ["inbox"]
        team(api, company, expect=403)


# Migration --------------------------------------------------------------------------------------

@pytest.fixture(params=["sqlite", "postgres"])
def old_database(request, tmp_path):
    """An engine at the revision before Team Members, on each supported database."""
    url = f"sqlite:///{tmp_path / 'old.sqlite'}" if request.param == "sqlite" else request.getfixturevalue("postgres_url")
    engine = create_engine(url)
    upgrade_to_head(engine, "0006_company_identifiers")
    yield engine
    engine.dispose()


def test_the_migration_keeps_every_existing_row(old_database):
    json_data = '{"name":"Existing Co"}'
    with old_database.begin() as connection:
        connection.execute(text("INSERT INTO organizations VALUES ('o1','Existing Firm','active','2026-01-01','2026-01-01')"))
        connection.execute(text("INSERT INTO users (id,email,display_name,status,created_at,updated_at,password_hash) "
                                "VALUES ('u1','client@existing.test','Existing Client','active','2026-01-01','2026-01-01','$argon2id$hash')"))
        connection.execute(text("INSERT INTO records (owner,id,kind,data,revision,updated) VALUES ('o1','co1','company',:d,1,'2026-01-01')"), {"d": json_data})
        connection.execute(text("INSERT INTO organization_memberships VALUES ('m1','o1','u1','client','Client','active',0,'2026-01-01','2026-01-01')"))
        connection.execute(text("INSERT INTO company_access VALUES ('o1','m1','co1','2026-01-01')"))
    upgrade_to_head(old_database)
    with old_database.connect() as connection:
        user = connection.execute(text("SELECT email, password_hash, username FROM users WHERE id = 'u1'")).one()
        access = connection.execute(text("SELECT company_id, client_role, client_modules FROM company_access WHERE membership_id = 'm1'")).one()
    assert tuple(user) == ("client@existing.test", "$argon2id$hash", None)
    assert tuple(access) == ("co1", None, None)  # a primary client contact: nothing about them changed
    with pytest.raises(IntegrityError), old_database.begin() as connection:
        connection.execute(text("UPDATE company_access SET client_role = 'Secretary' WHERE membership_id = 'm1'"))
    with old_database.begin() as connection:
        connection.execute(text("UPDATE users SET username = 'First.Name' WHERE id = 'u1'"))
        connection.execute(text("INSERT INTO users (id,display_name,status,created_at,updated_at) VALUES ('u2','Second','active','2026-01-01','2026-01-01')"))
    with pytest.raises(IntegrityError), old_database.begin() as connection:
        connection.execute(text("UPDATE users SET username = 'first.name' WHERE id = 'u2'"))
