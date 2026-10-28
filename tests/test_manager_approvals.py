"""Managers answer approval requests for their own team; finance asks and still decides (docs/adr/0003).

Finance raises a request on an item; it goes to the claimant's own manager. That manager answers once,
the answer is logged (approval/rejection, actor human:<manager>, its own run id) before the request
changes, and finance sees it on the claim page. Another manager gets a 404, the owner of the claim is
refused, and an audit outage changes nothing.
"""

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, manager, plans

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

from conftest import install_test_users, login_as

CLAIM, ITEM = "CLM-2026-0101", "IT-1"  # EMP-001, whose manager is EMP-100
OTHER_TEAM_CLAIM = "CLM-2026-0104"  # EMP-003, whose manager is EMP-101
NO_MANAGER_CLAIM = "CLM-2026-0120"  # EMP-101, no manager in the directory


@pytest.fixture
def client(tmp_path, monkeypatch):
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    for name in ("APP_ENV", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    web.reset_all()
    plans.STATE.clear()
    auth.reset_attempts()


def as_(c, emp):
    assert login_as(c, emp, "/").status_code == 303
    return c


def ask(c, cid=CLAIM, item=ITEM, reason="Hotel di atas plafon tanpa memo"):
    as_(c, "EMP-102")
    return c.post(f"/admin/claims/{cid}/items/{item}/manager", data={"reason": reason}, follow_redirects=False)


def events(rid):
    return plans.log().list(manager.run_id(rid))


def test_finance_asks_the_claimants_own_manager(client):
    r = ask(client)
    assert r.status_code == 303
    (req,) = web.store.approval_requests
    assert (req["manager_id"], req["claim_id"], req["item_id"], req["status"]) == ("EMP-100", CLAIM, ITEM, "pending")
    page = client.get(f"/admin/claims/{CLAIM}").text
    assert "Menunggu jawaban atasan" in page and f"/admin/claims/{CLAIM}/items/{ITEM}/manager" not in page
    assert ask(client).status_code == 303 and len(web.store.approval_requests) == 1  # one open request per item
    as_(client, "EMP-100")
    assert "Hotel di atas plafon tanpa memo" in client.get("/manager").text
    as_(client, "EMP-101")
    assert "Hotel di atas plafon tanpa memo" not in client.get("/manager").text


def test_no_manager_no_request(client):
    r = ask(client, NO_MANAGER_CLAIM)
    assert r.status_code == 303 and web.store.approval_requests == []
    assert f"/items/{ITEM}/manager" not in client.get(f"/admin/claims/{NO_MANAGER_CLAIM}").text


def test_manager_approves_once_and_finance_sees_it(client):
    ask(client)
    rid = web.store.approval_requests[0]["id"]
    as_(client, "EMP-100")
    page = client.get("/manager").text
    assert f"/manager/requests/{rid}" in page and "Setujui" in page
    r = client.post(f"/manager/requests/{rid}", data={"decision": "approved", "note": "Sudah saya cek"},
                    follow_redirects=False)
    assert r.status_code == 303
    req = web.store.approval_requests[0]
    assert (req["status"], req["answered_by"], req["note"]) == ("approved", "EMP-100", "Sudah saya cek")
    (ev,) = events(rid)
    assert (ev["event_type"], ev["actor"], ev["action_id"]) == ("approval", "human:EMP-100", rid)
    assert ev["payload"]["claim_id"] == CLAIM and ev["payload"]["decision"] == "approved"
    # A second answer, even the opposite one, changes nothing.
    r = client.post(f"/manager/requests/{rid}", data={"decision": "rejected"}, follow_redirects=False)
    assert "sudah dijawab" in r.headers["location"].replace("%20", " ")
    assert web.store.approval_requests[0]["status"] == "approved" and len(events(rid)) == 1
    assert f"/manager/requests/{rid}" not in client.get("/manager").text
    # Finance sees the answer; the claim itself is untouched.
    status_before = web.store.get_claim(CLAIM)["status"]
    as_(client, "EMP-102")
    page = client.get(f"/admin/claims/{CLAIM}").text
    assert "Disetujui atasan" in page and "Sudah saya cek" in page
    assert web.store.get_claim(CLAIM)["status"] == status_before
    assert A.unmatched_executions(plans.log().list()) == []


def test_reject_is_logged_as_rejection(client):
    ask(client)
    rid = web.store.approval_requests[0]["id"]
    as_(client, "EMP-100")
    client.post(f"/manager/requests/{rid}", data={"decision": "rejected"})
    assert web.store.approval_requests[0]["status"] == "rejected"
    assert [e["event_type"] for e in events(rid)] == ["rejection"]


def test_other_manager_and_bad_input(client):
    ask(client)
    rid = web.store.approval_requests[0]["id"]
    as_(client, "EMP-101")
    r = client.post(f"/manager/requests/{rid}", data={"decision": "approved"})
    assert r.status_code == 404 and r.json()["detail"] == f"Unknown request {rid}"
    assert client.post("/manager/requests/APR-999", data={"decision": "approved"}).json()["detail"] == \
        "Unknown request APR-999"
    as_(client, "EMP-100")
    assert client.post(f"/manager/requests/{rid}", data={"decision": "maybe"}).status_code == 400
    assert web.store.approval_requests[0]["status"] == "pending" and events(rid) == []


@pytest.mark.parametrize("emp", ["EMP-001", "EMP-102"])
def test_only_managers_may_answer(client, emp):
    ask(client)
    rid = web.store.approval_requests[0]["id"]
    as_(client, emp)
    assert client.post(f"/manager/requests/{rid}", data={"decision": "approved"}).status_code == 403
    assert web.store.approval_requests[0]["status"] == "pending"


def test_managers_cannot_ask(client):
    as_(client, "EMP-100")
    r = client.post(f"/admin/claims/{CLAIM}/items/{ITEM}/manager", data={"reason": "x"})
    assert r.status_code == 403 and web.store.approval_requests == []


def test_own_claim_is_refused_and_logged(client):
    # request_approval never addresses a manager about their own claim; a request built by hand must still fail.
    web.store.approval_requests.append({"id": "APR-900", "manager_id": "EMP-101", "claim_id": NO_MANAGER_CLAIM,
                                        "item_id": "IT-1", "reason": "", "status": "pending"})
    as_(client, "EMP-101")
    r = client.post("/manager/requests/APR-900", data={"decision": "approved"}, follow_redirects=False)
    assert r.status_code == 303
    assert web.store.approval_requests[0]["status"] == "pending"
    (ev,) = events("APR-900")
    assert (ev["event_type"], ev["actor"], ev["payload"]["reason"]) == ("access_denied", "human:EMP-101", "self_approval")


def test_audit_outage_changes_nothing(client, monkeypatch):
    ask(client)
    rid = web.store.approval_requests[0]["id"]
    as_(client, "EMP-100")

    class Down:
        def append(self, event):
            raise RuntimeError("audit down")

        def list(self, run_id=None):
            return []

    monkeypatch.setattr(plans, "log", lambda: Down())
    r = client.post(f"/manager/requests/{rid}", data={"decision": "approved"})
    assert r.status_code == 503
    assert web.store.approval_requests[0]["status"] == "pending"


def test_reset_clears_requests(client):
    ask(client)
    web.reset_all()
    assert web.store.approval_requests == []
