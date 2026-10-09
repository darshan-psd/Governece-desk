"""Client portal, onboarding, compliance, threads, invoices and payments (ported from tests/client-portal.mjs)."""
import io
import zipfile
from datetime import date

import pytest

PROFILE = {
    "name": "Portal validation", "entityType": "Private limited company", "registration": "U72900TN2026PTC123456",
    "incorporationDate": "2026-01-05", "email": "client@example.invalid", "phone": "+91 9000000000", "address": "1 Test Road",
    "city": "Chennai", "state": "Tamil Nadu", "pincode": "600001", "businessActivity": "Software services", "contact": "Test client",
    "pan": "ABCDE1234F", "authorisedCapital": "100000.00", "paidCapital": "10000.00",
    "directors": [{"name": "Test director", "din": "12345678", "email": "director@example.invalid"}],
    "shareholders": [{"name": "Test shareholder", "holding": "100"}], "confirmed": True,
}


def portal_get(api, record_id):
    s = api.portal()
    for key in ("companies", "cases", "documents", "obligations", "threads", "invoices", "payments"):
        for item in s[key]:
            if item["id"] == record_id:
                return item
    return None


def portal_act(api, record_id, action, body=None, expect=200):
    r = portal_get(api, record_id)
    assert r, f"missing {record_id}"
    return api.portal({"id": record_id, "revision": r["revision"], "company": r.get("company") or r["id"], "action": action, **(body or {})}, expect)


def portal_upload(api, company, shared=True, content=b"Valid evidence", previous=""):
    return api.upload(company, previous=previous, content=content, filename="certificate.txt", category="Incorporation certificate", shared=str(shared).lower())


@pytest.fixture
def portal(api):
    c = api.portal({"action": "companyCreate", "clientName": "Portal test client", "data": PROFILE})["id"]
    c2 = api.portal({"action": "companyCreate", "clientName": "Other client", "data": {"name": "Other private company"}})["id"]
    client = next(m["id"] for m in api.state()["members"] if m["role"] == "Client" and c in m["companies"])
    secret = portal_upload(api, c, shared=False, content=b"Internal only")
    public = portal_upload(api, c)
    foreign = portal_upload(api, c2)
    return api, c, c2, client, secret, public, foreign


def test_onboarding_profile_compliance_and_threads(portal):
    api, c, c2, client, secret, public, foreign = portal
    onboard = f"onboard-{c}"

    api.role(client)
    state = api.portal()
    assert len(state["companies"]) == 1
    assert not any(d["id"] == secret for d in state["documents"])
    portal_act(api, c, "companySubmit", {"data": {**PROFILE, "registration": "bad"}}, 400)
    portal_act(api, c, "companySubmit", {"data": {**PROFILE, "pincode": "bad"}}, 400)
    portal_act(api, c, "companySubmit", {"data": {**PROFILE, "shareholders": [{"name": "Too much", "holding": 101}]}}, 400)
    portal_act(api, c, "companySubmit", {"data": {**PROFILE, "paidCapital": "1000000"}}, 400)
    portal_act(api, c, "companySubmit", {"data": PROFILE})
    portal_act(api, c, "companyReview", {"status": "Verified"}, 403)
    api.portal({"action": "stageUpdate", "company": c, "id": onboard, "revision": portal_get(api, onboard)["revision"],
                "stage": "engagement", "status": "Verified", "evidence": [public]}, 403)
    created = api.portal({"action": "companyCreate", "data": {"name": "Client-created company"}})["id"]
    assert any(x["id"] == created for x in api.portal()["companies"])
    api.portal({"action": "companySave", "id": c2, "revision": 1, "data": PROFILE}, 404)
    assert api.request("GET", f"/api/portal?export=bundle&company={c2}").status_code == 404

    api.role("secretary")
    portal_act(api, c, "companyReview", {"status": "Verified"})
    portal_act(api, onboard, "stageUpdate", {"stage": "registration", "status": "Verified", "evidence": [public]}, 400)
    portal_act(api, onboard, "stageUpdate", {"stage": "engagement", "status": "Verified", "evidence": []}, 400)
    portal_act(api, onboard, "stageUpdate", {"stage": "engagement", "status": "Verified", "evidence": [foreign]}, 400)
    portal_act(api, onboard, "stageUpdate", {"stage": "engagement", "status": "Verified", "evidence": [public]})
    for stage in ("verification", "constitution", "registration", "postregistration"):
        portal_act(api, onboard, "stageUpdate", {"stage": stage, "status": "Verified", "evidence": [public]})
    portal_act(api, onboard, "stageUpdate", {"stage": "annual", "status": "Verified", "evidence": [public]}, 400)
    api.portal({"action": "annualPlan", "company": c, "period": "2026-99"}, 400)
    api.portal({"action": "annualPlan", "company": c, "period": "2026-27"})
    api.portal({"action": "annualPlan", "company": c, "period": "2026-27"})  # idempotent
    items = [o for o in api.portal()["obligations"] if o["company"] == c]
    assert len(items) == 6
    first = items[0]
    assert first["title"].startswith("Financial statements")
    today = date.today().isoformat()
    portal_act(api, first["id"], "obligationSave", {"data": {**first, "due": "2026-02-30", "status": "In progress"}}, 400)
    portal_act(api, first["id"], "obligationSave", {"data": {**first, "due": today, "status": "Completed", "evidence": [public]}}, 400)
    portal_act(api, first["id"], "obligationSave", {"data": {**first, "due": today, "status": "In progress"}})
    portal_act(api, onboard, "stageUpdate", {"stage": "annual", "status": "Verified", "evidence": [public]})
    assert portal_get(api, onboard)["status"] == "Completed"

    api.role(client)
    topic = api.portal({"action": "threadCreate", "company": c, "subject": "Annual compliance query",
                        "content": "Please confirm the documents needed.", "attachments": [public]})["id"]
    portal_act(api, topic, "threadReply", {"content": "Cross-company attachment", "attachments": [foreign]}, 400)
    portal_act(api, topic, "threadReply", {"content": "Internal attachment", "attachments": [secret]}, 400)
    api.role("secretary")
    assert portal_get(api, topic)["unread"]
    portal_act(api, topic, "threadReply", {"content": "Please upload the signed accounts.", "attachments": [public]})
    api.role(client)
    assert portal_get(api, topic)["unread"]
    portal_act(api, topic, "threadRead")
    assert portal_get(api, topic)["unread"] is False
    portal_act(api, topic, "threadStatus", {"status": "Resolved"}, 403)
    assert api.request("GET", f"/api/v2-files?id={secret}").status_code == 404

    bundle = api.request("GET", f"/api/portal?export=bundle&company={c}")
    assert bundle.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()
    assert "document-manifest.json" in names
    assert any(public in n for n in names) and not any(secret in n for n in names)
    calendar = api.request("GET", f"/api/portal?export=calendar&company={c}").text
    assert "BEGIN:VALARM" in calendar and "Financial statements" in calendar
    assert any(a["company"] == c for a in api.portal()["alerts"])

    # Concurrency on threads, and cross-company record access.
    stale = portal_get(api, topic)
    portal_act(api, topic, "threadReply", {"content": "Follow-up message"})
    api.portal({"action": "threadReply", "company": c, "id": topic, "revision": stale["revision"], "content": "Stale"}, 409)
    api.role("owner")
    api.portal({"action": "threadReply", "company": c2, "id": topic, "revision": portal_get(api, topic)["revision"], "content": "Wrong company"}, 404)

    # A new version of verified evidence reopens the onboarding milestone.
    portal_upload(api, c, content=b"Revised certificate", previous=public)
    assert portal_get(api, onboard)["stages"][0]["status"] == "Review required"


def test_invoices_payments_and_receipts(portal):
    api, c, _c2, client, _secret, public, _foreign = portal
    today = date.today().isoformat()
    invoice = {"number": "TEST-1", "due": today, "items": [{"description": "Annual compliance service <script>", "quantity": 2, "rate": "1250.25"}],
               "tax": "0", "seller": {"name": "Test Secretary LLP", "address": "2 Service Street", "email": "secretary@example.invalid",
                                      "paymentInstructions": "Contact our office for bank details."}}
    api.portal({"action": "invoiceCreate", "company": c, "data": {**invoice, "items": [{"description": "Bad", "quantity": 1, "rate": "-1"}]}}, 400)
    inv = api.portal({"action": "invoiceCreate", "company": c, "data": invoice})["id"]
    assert portal_get(api, inv)["total"] == 250050

    api.role(client)
    assert not any(i["id"] == inv for i in api.portal()["invoices"])  # drafts are internal
    assert api.request("GET", f"/api/portal?export=invoice&company={c}&id={inv}").status_code == 404
    api.portal({"action": "invoiceCreate", "company": c, "data": invoice}, 403)

    api.role("owner")
    portal_act(api, inv, "invoiceIssue")
    duplicate = api.portal({"action": "invoiceCreate", "company": c, "data": invoice})["id"]
    portal_act(api, duplicate, "invoiceIssue", expect=409)  # invoice numbers are unique

    api.role(client)
    assert portal_get(api, inv)["balance"] == 250050
    portal_act(api, inv, "paymentReport", {"amount": "2600", "reference": "REF1", "paidAt": today, "evidence": [public]}, 400)
    portal_act(api, inv, "paymentReport", {"amount": "1000", "reference": "REF1", "paidAt": today, "evidence": []}, 400)
    portal_act(api, inv, "paymentReport", {"amount": "1000", "reference": "REF1", "paidAt": "2999-01-01", "evidence": [public]}, 400)
    pay = portal_act(api, inv, "paymentReport", {"amount": "1000", "reference": "REF1", "paidAt": today, "evidence": [public]})["id"]
    assert portal_get(api, inv)["balance"] == 250050
    assert portal_get(api, inv)["pending"] == 100000
    portal_act(api, inv, "paymentReport", {"amount": "1600", "reference": "REF2", "paidAt": today, "evidence": [public]}, 400)  # overpayment
    portal_act(api, inv, "paymentReport", {"amount": "100", "reference": "ref1", "paidAt": today, "evidence": [public]}, 400)  # duplicate ref
    portal_act(api, pay, "paymentReview", {"status": "Confirmed"}, 403)  # clients cannot confirm
    assert api.request("GET", f"/api/portal?export=receipt&company={c}&id={pay}").status_code == 404

    api.role("owner")
    portal_act(api, inv, "invoiceVoid", {"reason": "Has payment"}, 400)
    portal_act(api, inv, "invoiceIssue", expect=400)  # issued invoices are locked
    portal_act(api, pay, "paymentReview", {"status": "Confirmed"})
    portal_act(api, pay, "paymentReview", {"status": "Confirmed"}, 400)
    assert portal_get(api, inv)["balance"] == 150050  # partial payment

    api.role(client)
    receipt = api.request("GET", f"/api/portal?export=receipt&company={c}&id={pay}").text
    assert "Payment receipt" in receipt and "INR 1,000.00" in receipt
    html = api.request("GET", f"/api/portal?export=invoice&company={c}&id={inv}").text
    assert "&lt;script&gt;" in html and "INR 2,500.50" in html
    last = portal_act(api, inv, "paymentReport", {"amount": "1500.50", "reference": "REF3", "paidAt": today, "evidence": [public]})["id"]
    api.role("secretary")
    portal_act(api, last, "paymentReview", {"status": "Rejected"}, 400)
    portal_act(api, last, "paymentReview", {"status": "Rejected", "note": "Reference not found"})
    assert portal_get(api, inv)["pending"] == 0
