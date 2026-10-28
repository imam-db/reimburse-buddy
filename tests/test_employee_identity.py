"""The acting employee comes only from the login session (or REIMBURSE_DEV_EMPLOYEE with the gate off).

No /employee route takes an employee ID from the query string or a form, and every route checks that the claim
or draft it touches belongs to the acting employee, whether or not EMPLOYEE_QA is on (ADR 0002).
"""

import pytest
from conftest import install_test_users, login_as
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans, qa

A, B = "EMP-001", "EMP-003"
A_CLAIM, B_CLAIM = "CLM-2026-0101", "CLM-2026-0104"
NEW_CLAIM = {"purpose": "p", "destination": "Bandung", "start_date": "2026-10-20", "end_date": "2026-10-21",
             "items": "taxi, 2026-10-20, Taxi, 150000, no"}
TRIP = {"purpose": "Visit", "destination": "Surabaya", "start_date": "2026-10-02", "end_date": "2026-10-04"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    monkeypatch.delenv("REIMBURSE_DEV_EMPLOYEE", raising=False)
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    web.reset_all()
    qa.STATE.clear()
    yield monkeypatch
    qa.STATE.clear()
    plans.STATE.clear()
    web.reset_all()


def client_for(emp):
    install_test_users()
    c = TestClient(web.app)
    assert login_as(c, emp, "/employee").status_code == 303
    return c


def make_draft(c) -> str:
    r = c.post("/employee/drafts", data=TRIP, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def test_query_emp_is_ignored(env):
    c = client_for(A)
    page = c.get(f"/employee?emp={B}").text
    assert A_CLAIM in page and B_CLAIM not in page
    assert "Signed in as" not in page and "emp=" not in page and 'name="emp"' not in page
    new = c.get(f"/employee/new?emp={B}").text
    assert "Rina" in new and "Sari" not in new and "emp=" not in new and "name=\"emp\"" not in new


def test_posted_emp_never_names_the_owner(env):
    c = client_for(A)
    r = c.post("/employee/claims", data={"emp": B, **NEW_CLAIM}, follow_redirects=False)
    assert r.status_code == 303 and "emp=" not in r.headers["location"]
    assert web.store.get_claim("CLM-2026-W001")["employee_id"] == A
    r = c.post("/employee/forward", data={"emp": B, "subject": "s"}, follow_redirects=False)
    assert r.status_code == 303
    assert web.store.inbox[-1]["owner_id"] == A
    did = make_draft_with_emp(c)
    assert web.DRAFTS[did]["emp"] == A


def make_draft_with_emp(c) -> str:
    r = c.post("/employee/drafts", data={"emp": B, **TRIP}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


@pytest.mark.parametrize("qa_on", [False, True])
def test_other_employees_objects_are_404(env, qa_on):
    if qa_on:
        env.setenv("EMPLOYEE_QA", "1")
    b = client_for(B)
    b_draft = make_draft(b)
    b.post("/employee/claims", data=NEW_CLAIM)
    b_new = "CLM-2026-W001"
    a = client_for(A)
    missing = a.get("/employee/claims/CLM-NOPE")
    assert missing.status_code == 404
    for cid in (B_CLAIM, b_new):
        r = a.get(f"/employee/claims/{cid}")
        assert r.status_code == 404 and cid in r.text
        assert r.json()["detail"] == f"Unknown claim {cid}"
        up = a.post(f"/employee/claims/{cid}/items/IT-1/files", files={"file": ("r.pdf", b"%PDF", "application/pdf")})
        assert up.status_code == 404
    assert a.get(f"/employee/drafts/{b_draft}").status_code == 404
    assert a.post(f"/employee/drafts/{b_draft}", data={"action": "submit"}).status_code == 404
    assert a.get("/employee/drafts/nope").json()["detail"].replace("nope", "X") ==         a.get(f"/employee/drafts/{b_draft}").json()["detail"].replace(b_draft, "X")
    inbox_before = len(web.store.inbox)
    fwd = a.post("/employee/forward", data={"subject": "s", "claim_id": B_CLAIM, "item_id": "IT-1"},
                 files={"file": ("r.pdf", b"%PDF", "application/pdf")})
    assert fwd.status_code == 404 and len(web.store.inbox) == inbox_before
    assert b_draft in web.DRAFTS and web.store.get_claim(B_CLAIM)["employee_id"] == B
    # The owner still gets in.
    assert b.get(f"/employee/claims/{B_CLAIM}").status_code == 200
    assert b.get(f"/employee/drafts/{b_draft}").status_code == 200


def test_signed_out_get_redirects_and_post_is_401(env):
    install_test_users()
    c = TestClient(web.app)
    r = c.get("/employee", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    for url, data in (("/employee/claims", NEW_CLAIM), ("/employee/forward", {"subject": "s"}),
                      ("/employee/drafts", TRIP), (f"/employee/claims/{A_CLAIM}/items/IT-1/files", None),
                      ("/employee/drafts/abc", {"action": "save"})):
        r = c.post(url, data=data) if data else c.post(url, files={"file": ("r.pdf", b"%PDF", "application/pdf")})
        assert r.status_code == 401, url


def test_gate_off_dev_employee(env):
    c = TestClient(web.app)
    assert not auth.enabled()
    assert "DEV ONLY" in c.get("/employee").text
    assert "DEV ONLY" in c.get("/employee/new").text
    assert "DEV ONLY" in c.get(f"/employee/claims/{A_CLAIM}").text
    assert c.post("/employee/claims", data=NEW_CLAIM).status_code == 401
    env.setenv("REIMBURSE_DEV_EMPLOYEE", f" {B} ")
    page = c.get(f"/employee?emp={A}").text
    assert B_CLAIM in page and A_CLAIM not in page
    assert c.get(f"/employee/claims/{A_CLAIM}").status_code == 404
    assert c.get(f"/employee/claims/{B_CLAIM}").status_code == 200
    env.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-NOBODY")
    assert "DEV ONLY" in c.get("/employee").text


def test_gate_on_ignores_dev_employee(env):
    env.setenv("REIMBURSE_DEV_EMPLOYEE", B)
    a = client_for(A)
    page = a.get("/employee").text
    assert A_CLAIM in page and B_CLAIM not in page
    install_test_users()
    anon = TestClient(web.app)
    assert anon.get("/employee", follow_redirects=False).status_code == 303
    assert anon.post("/employee/claims", data=NEW_CLAIM).status_code == 401


def test_reviewer_session_acts_as_its_own_employee_id(env):
    c = client_for("EMP-100")
    r = c.post("/employee/claims", data=NEW_CLAIM, follow_redirects=False)
    assert r.status_code == 303
    assert web.store.get_claim("CLM-2026-W001")["employee_id"] == "EMP-100"
