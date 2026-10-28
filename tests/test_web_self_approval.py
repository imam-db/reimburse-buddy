"""A reviewer may not approve their own request or claim.

The identity comes from the login (auth.reviewer_of): the account signed in as EMP-100, EMP-101 or EMP-102,
and the same person who requested a plan, or who owns a claim, is refused on every approval path. The
refusals are fail closed (nothing is stored, nothing executes) and land in the audit log as
`access_denied` with actor `human:<reviewer>`.

The intake also records who submitted the request (`submitted_by`): a reviewer may name anyone as the
requester, so the submitter may not approve it either, even when the requester is somebody else.

Rejections, notes and `on_hold` stay available: they cannot approve anything.
"""

import copy
import json
import re
import threading

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, intake, plans
from reimburse_web.agent_bridge import Run

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

from conftest import TEST_PASSWORD as PW, install_test_users, login_as
REQ = "Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up Jumat siang."
OWN_CLAIM = "CLM-2026-9901"  # a copy of CLM-2026-0103 (Medan hotel over cap) owned by EMP-100
SHOWCASE_CLAIM = "CLM-2026-0120"  # data/claims.json, batch 2026-W44, owned by EMP-101


def _own_claim() -> dict:
    src = next(c for c in web.store.claims if c["id"] == "CLM-2026-0103")
    claim = copy.deepcopy(src)
    claim.update(id=OWN_CLAIM, employee_id="EMP-100", purpose="Own claim (self-approval test)")
    return claim


@pytest.fixture
def client(tmp_path, monkeypatch):
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    for name in ("APP_ENV", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    clock = {"now": intake._utcnow()}
    monkeypatch.setattr(intake, "_utcnow", lambda: clock["now"])
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    intake.reset()
    auth.reset_attempts()
    web.store.claims.append(_own_claim())
    yield TestClient(web.app)
    web.reset_all()
    plans.STATE.clear()
    intake.reset()
    auth.reset_attempts()


def login(c, reviewer):
    r = login_as(c, reviewer)
    assert r.status_code == 303, r.text


def start_plan(c, requester="EMP-101") -> str:
    r = c.post("/admin/intake", data={"request": REQ, "document": "", "requester": requester},
               follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[1]
    r = c.post(f"/admin/plans/{run_id}/answer", data={"answer": "EMP-202"}, follow_redirects=False)
    assert r.status_code == 303
    return run_id


def events(run_id):
    return plans.log().list(run_id)


def approve_form(c, run_id):
    plan = plans.store().load_plan(run_id)
    data = {"plan_id": plan["plan_id"]}
    for action in plan["actions"]:
        data[f"decision:{action['action_id']}"] = "approve"
    return data


# --- plans ---------------------------------------------------------------------------------
def test_requester_cannot_approve_their_own_plan(client):
    c = client
    login(c, "EMP-101")
    run_id = start_plan(c, requester="EMP-101")

    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(c, run_id), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin/plans/{run_id}?msg=")

    plan = plans.store().load_plan(run_id)
    assert plans.store().load_decisions(run_id, plan["version"]) is None  # nothing stored
    evs = events(run_id)
    assert not [e for e in evs if e["event_type"] in ("approval", "execution")]
    assert not (plans.store().root / run_id / "mock_office.json").exists()  # nothing ran

    denied = [e for e in evs if e["event_type"] == "access_denied"]
    assert len(denied) == 1
    assert denied[0]["actor"] == "human:EMP-101"
    assert denied[0]["payload"]["reason"] == "self_approval"
    assert denied[0]["payload"]["requester"] == "EMP-101"


def test_requester_can_still_reject_their_own_plan(client):
    c = client
    login(c, "EMP-101")
    run_id = start_plan(c, requester="EMP-101")
    plan = plans.store().load_plan(run_id)
    data = {"plan_id": plan["plan_id"]}
    for action in plan["actions"]:
        data[f"decision:{action['action_id']}"] = "reject"

    r = c.post(f"/admin/plans/{run_id}/decide", data=data, follow_redirects=False)
    assert r.status_code == 303 and "Keputusan" in r.headers["location"]
    saved = plans.store().load_decisions(run_id, plan["version"])
    assert saved and set(saved["decisions"].values()) == {"reject"}
    assert saved["actor"] == "human:EMP-101"
    assert [e for e in events(run_id) if e["event_type"] == "rejection"]
    assert not [e for e in events(run_id) if e["event_type"] == "execution"]


def test_another_reviewer_can_approve_and_the_actor_is_recorded(client):
    c = client
    login(c, "EMP-101")
    run_id = start_plan(c, requester="EMP-101")  # requested and submitted by EMP-101
    login(c, "EMP-100")  # neither the requester nor the submitter

    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(c, run_id), follow_redirects=False)
    assert r.status_code == 303 and "Keputusan" in r.headers["location"]

    plan = plans.store().load_plan(run_id)
    saved = plans.store().load_decisions(run_id, plan["version"])
    assert saved["actor"] == "human:EMP-100"
    approvals = [e for e in events(run_id) if e["event_type"] == "approval"]
    assert approvals and all(e["actor"] == "human:EMP-100" for e in approvals)
    executions = [e for e in events(run_id) if e["event_type"] == "execution"]
    assert len(executions) == len(plan["actions"])  # the live graph resumed and ran the mock tools
    assert not [e for e in events(run_id) if e["event_type"] == "access_denied"]


def test_submitter_cannot_approve_a_request_filed_for_someone_else(client):
    """The demo flow: EMP-100 submits Example Test 1 with EMP-101 as the requester. EMP-100 may not
    approve it (they filed it), EMP-101 may not either (they are the requester), EMP-102 can."""
    c = client
    login(c, "EMP-100")
    run_id = start_plan(c, requester="EMP-101")
    plan = plans.store().load_plan(run_id)
    assert plan["submitted_by"] == "EMP-100"
    assert plans.store().load_request(run_id)["submitted_by"] == "EMP-100"

    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(c, run_id), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin/plans/{run_id}?msg=")
    assert plans.store().load_decisions(run_id, plan["version"]) is None  # nothing stored
    denied = [e for e in events(run_id) if e["event_type"] == "access_denied"]
    assert [(e["actor"], e["payload"]["blocked"]) for e in denied] == [("human:EMP-100", "submitted_by")]
    assert denied[0]["payload"]["reason"] == "self_approval"
    assert denied[0]["payload"]["submitted_by"] == "EMP-100"
    assert denied[0]["payload"]["requester"] == "EMP-101"

    # The requester named on the form is refused as well, even though they did not submit it.
    login(c, "EMP-101")
    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(c, run_id), follow_redirects=False)
    assert r.headers["location"].startswith(f"/admin/plans/{run_id}?msg=")
    assert plans.store().load_decisions(run_id, plan["version"]) is None

    # The third reviewer (Finance) can approve it.
    login(c, "EMP-102")
    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(c, run_id), follow_redirects=False)
    assert r.status_code == 303 and "Keputusan" in r.headers["location"]
    saved = plans.store().load_decisions(run_id, plan["version"])
    assert saved["actor"] == "human:EMP-102"
    executions = [e for e in events(run_id) if e["event_type"] == "execution"]
    assert len(executions) == len(plan["actions"]) == 2
    assert all(e["actor"] == "human:EMP-102" for e in events(run_id) if e["event_type"] == "approval")


def test_plan_page_shows_who_submitted_it(client):
    c = client
    login(c, "EMP-100")
    run_id = start_plan(c, requester="EMP-101")
    page = c.get(f"/admin/plans/{run_id}").text
    assert "id=submitted-by" in page and "Diajukan oleh" in page and "Dewi Anggraini" in page
    assert "EMP-101" not in page.split("id=submitted-by")[1].split("</span>")[0]  # requester stays separate


def test_plan_page_disables_approve_for_the_requester_and_the_submitter(client):
    c = client
    login(c, "EMP-101")
    run_id = start_plan(c, requester="EMP-101")
    page = c.get(f"/admin/plans/{run_id}").text
    assert "id='self-approval'" in page
    assert re.search(r"value=approve disabled", page)
    assert "Tolak / Reject" in page

    # The submitter is blocked too, with its own wording.
    login(c, "EMP-100")
    run_id = start_plan(c, requester="EMP-101")
    page = c.get(f"/admin/plans/{run_id}").text
    assert "id='self-approval'" in page and "Anda yang mengajukan permintaan ini" in page
    assert re.search(r"value=approve disabled", page)

    # A reviewer who is neither sees the normal control.
    login(c, "EMP-102")
    page = c.get(f"/admin/plans/{run_id}").text
    assert "id='self-approval'" not in page
    assert re.search(r"name='decision:[^']+' value=approve>", page)


# --- claims --------------------------------------------------------------------------------
def test_claim_owner_cannot_approve_an_item(client):
    c = client
    login(c, "EMP-100")
    r = c.post(f"/admin/claims/{OWN_CLAIM}/decide",
               data={"item_id": "IT-1", "choice": "approve_capped"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin/claims/{OWN_CLAIM}?msg=")
    assert web.REVIEW.get(OWN_CLAIM, {}).get("decisions", {}) == {}  # nothing recorded
    denied = [e for e in plans.log().list(f"claim:{OWN_CLAIM}") if e["event_type"] == "access_denied"]
    assert denied and denied[0]["actor"] == "human:EMP-100"
    assert denied[0]["payload"]["reason"] == "self_approval"

    # A rejection is allowed, and a different reviewer may approve.
    r = c.post(f"/admin/claims/{OWN_CLAIM}/decide", data={"item_id": "IT-1", "choice": "reject"},
               follow_redirects=False)
    assert r.status_code == 303 and "msg=" not in r.headers["location"]
    assert web.REVIEW[OWN_CLAIM]["decisions"]["IT-1"] == "reject"


def test_claim_owner_cannot_approve_a_status_change_but_may_hold(client):
    c = client
    login(c, "EMP-100")
    r = c.post(f"/admin/claims/{OWN_CLAIM}/status",
               data={"status": "approved", "reviewer_confirmed": "true"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/admin/claims/{OWN_CLAIM}?msg=")
    assert web.store.get_claim(OWN_CLAIM)["status"] == "submitted"  # unchanged
    assert web.store.status_log == []

    r = c.post(f"/admin/claims/{OWN_CLAIM}/status",
               data={"status": "on_hold", "reviewer_confirmed": "true"}, follow_redirects=False)
    assert r.status_code == 303
    assert web.store.get_claim(OWN_CLAIM)["status"] == "on_hold"


def test_claim_owner_cannot_accept_their_own_evidence(client):
    c = client
    login(c, "EMP-100")
    web.store.inbox.append({"id": "MSG-W900", "owner_id": "EMP-100", "from": "x@example",
                            "date": "2026-10-20", "subject": "Struk", "body": "", "mailbox": web.MAILBOX,
                            "attachments": [{"name": "struk.png", "type": "receipt", "stored_as": "MSG-W900_struk.png"}]})
    web.REVIEW.setdefault(OWN_CLAIM, {"evidence": [], "decisions": {}, "pending": {}, "evidence_log": []})
    web.REVIEW[OWN_CLAIM]["pending"]["MSG-W900"] = {"item_id": "IT-1", "reason": "unreadable_file",
                                                    "state": "pending"}
    r = c.post(f"/admin/claims/{OWN_CLAIM}/evidence/MSG-W900/review", data={"verdict": "accept"},
               follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert web.REVIEW[OWN_CLAIM]["pending"]["MSG-W900"]["state"] == "pending"  # unchanged
    assert "MSG-W900" not in web.REVIEW[OWN_CLAIM]["evidence"]

    r = c.post(f"/admin/claims/{OWN_CLAIM}/evidence/MSG-W900/review", data={"verdict": "reject"},
               follow_redirects=False)
    assert web.REVIEW[OWN_CLAIM]["pending"]["MSG-W900"]["state"] == "rejected"


def test_batch_approve_refuses_the_whole_batch_when_it_holds_your_claim(client):
    c = client
    login(c, "EMP-100")
    cleared = web.triage_claims(web.store, web.BATCH)["auto_cleared"]
    r = c.post("/admin/batch/approve",
               data={"batch": web.BATCH, "claim_ids": cleared + [OWN_CLAIM], "reviewer_confirmed": "true"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin?batch={web.BATCH}&msg=")
    # Nothing in the batch was approved, not even the claims that are not the reviewer's.
    assert web.store.status_log == []
    assert all(web.store.get_claim(c)["status"] != "approved" for c in cleared)
    page = c.get(r.headers["location"]).text
    assert OWN_CLAIM in page and "tidak dapat menyetujui" in page

    # Without the reviewer's own claim the same batch still goes through.
    r = c.post("/admin/batch/approve",
               data={"batch": web.BATCH, "claim_ids": cleared, "reviewer_confirmed": "true"},
               follow_redirects=False)
    assert r.status_code == 303
    assert all(web.store.get_claim(c)["status"] == "approved" for c in cleared)


class _StubRun:
    """Stands in for an agent run: records what the web layer asked it to do."""

    def __init__(self, claim_id):
        self.claim_id = claim_id
        self.resumed = []
        self.replied = []

    def resume(self, approval_id, approved, reason="", reviewer=""):
        self.resumed.append((approval_id, approved, reason, reviewer))
        return True

    def reply(self, question_id, text, decisions, reviewer=""):
        self.replied.append((question_id, text, decisions, reviewer))
        return True

    def kill(self):
        return None


class _Pipe:
    """Captures what the web writes to the worker's stdin."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        return None


def _bare_run(events) -> Run:
    """A Run with only the state resume()/reply() touch: no subprocess, no reader thread."""
    run = Run.__new__(Run)
    run.events = events
    run.lock = threading.Lock()
    run.answered = set()
    run.proc = type("P", (), {"stdin": _Pipe()})()
    return run


def test_run_sends_the_reviewer_to_the_worker():
    """The worker cannot know who is signed in: the ID has to travel on the stdin answer."""
    run = _bare_run([{"type": "interrupt", "tool": "update_claim_status", "args": {},
                      "approval_id": "a1"}])
    assert run.resume("a1", True, "", reviewer="EMP-100")
    assert json.loads(run.proc.stdin.lines[0])["reviewer"] == "EMP-100"

    run = _bare_run([{"type": "question", "question_id": "q1", "text": "Which Budi?", "items": []}])
    assert run.reply("q1", "EMP-202", {"IT-1": "reject"}, reviewer="EMP-102")
    line = json.loads(run.proc.stdin.lines[0])
    assert line["reviewer"] == "EMP-102" and line["decisions"] == {"IT-1": "reject"}


def test_claim_owner_cannot_approve_a_pending_agent_write(client):
    c = client
    login(c, "EMP-100")
    run = _StubRun(OWN_CLAIM)
    web.INVESTIGATOR.runs["rid-self"] = run
    r = c.post("/admin/investigations/rid-self/resume",
               data={"approval_id": "a1", "approved": "true"}, follow_redirects=False)
    assert r.status_code == 303 and run.resumed == []  # the worker never saw the approval
    assert r.headers["location"].startswith(f"/admin/claims/{OWN_CLAIM}?msg=")

    # Declining is fine, and it names the reviewer who declined.
    r = c.post("/admin/investigations/rid-self/resume",
               data={"approval_id": "a1", "approved": "false"}, follow_redirects=False)
    assert r.status_code == 303 and run.resumed == [("a1", False, "", "EMP-100")]


def test_claim_owner_cannot_approve_items_from_an_agent_reply(client):
    c = client
    login(c, "EMP-100")
    run = _StubRun(OWN_CLAIM)
    web.INVESTIGATOR.runs["rid-reply"] = run
    r = c.post("/admin/investigations/rid-reply/reply",
               data={"question_id": "q1", "reply": "ok", "dec_IT-1": "approve_full"}, follow_redirects=False)
    assert r.status_code == 303 and run.replied == []
    assert r.headers["location"].startswith(f"/admin/claims/{OWN_CLAIM}?msg=")

    r = c.post("/admin/investigations/rid-reply/reply",
               data={"question_id": "q1", "reply": "tolak", "dec_IT-1": "reject"}, follow_redirects=False)
    assert r.status_code == 303 and run.replied == [("q1", "tolak", {"IT-1": "reject"}, "EMP-100")]


def test_claim_page_hides_the_approval_controls_for_the_owner(client):
    c = client
    login(c, "EMP-100")
    page = c.get(f"/admin/claims/{OWN_CLAIM}").text
    assert "id='self-approval'" in page
    assert "id='self-approval-status'" in page
    assert f"action='/admin/claims/{OWN_CLAIM}/decide'" not in page
    assert f'action="/admin/claims/{OWN_CLAIM}/status"' not in page

    login(c, "EMP-101")  # a different reviewer sees the controls
    page = c.get(f"/admin/claims/{OWN_CLAIM}").text
    assert "id='self-approval'" not in page
    assert f'action="/admin/claims/{OWN_CLAIM}/status"' in page


def test_second_batch_holds_one_claim_owned_by_a_reviewer(client):
    """The demo batch keeps its numbers; the self-approval showcase lives in its own batch."""
    c = client
    login(c, "EMP-100")
    w43 = web.triage_claims(web.store, web.BATCH)
    assert (w43["claims"], len(w43["auto_cleared"]), len(w43["exceptions"])) == (10, 7, 3)
    w44 = web.triage_claims(web.store, web.OWN_BATCH)
    assert w44["claims"] == 1 and [e["claim_id"] for e in w44["exceptions"]] == [SHOWCASE_CLAIM]
    page = c.get(f"/admin?batch={web.OWN_BATCH}").text
    assert web.OWN_BATCH in page and SHOWCASE_CLAIM in page


def test_showcase_claim_blocks_its_owner_and_lets_another_reviewer_in(client):
    c = client
    login(c, "EMP-101")  # the owner
    page = c.get(f"/admin/claims/{SHOWCASE_CLAIM}").text
    assert "id='self-approval'" in page
    assert f"action='/admin/claims/{SHOWCASE_CLAIM}/decide'" not in page

    r = c.post(f"/admin/claims/{SHOWCASE_CLAIM}/decide",
               data={"item_id": "IT-2", "choice": "approve_capped"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert web.REVIEW.get(SHOWCASE_CLAIM, {}).get("decisions", {}) == {}

    login(c, "EMP-102")
    page = c.get(f"/admin/claims/{SHOWCASE_CLAIM}").text
    assert "id='self-approval'" not in page
    assert f"action='/admin/claims/{SHOWCASE_CLAIM}/decide'" in page


def test_without_a_login_the_actor_is_unchanged(client, monkeypatch):
    """The gate is off in dev: decisions stay attributed to human:reviewer, and no submitter is recorded,
    exactly as before this change."""
    auth.set_store(None)
    c = TestClient(web.app)
    run_id = start_plan(c, requester="EMP-101")
    plan = plans.store().load_plan(run_id)
    assert "submitted_by" not in plan
    assert "submitted_by" not in plans.store().load_request(run_id)
    data = {"plan_id": plan["plan_id"]}
    for action in plan["actions"]:
        data[f"decision:{action['action_id']}"] = "approve"
    c.post(f"/admin/plans/{run_id}/decide", data=data, follow_redirects=False)
    saved = plans.store().load_decisions(run_id, plan["version"])
    assert saved["actor"] == "human:reviewer"
