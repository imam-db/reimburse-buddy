"""Tests for four security leads from a source-only audit. Each asserts the SECURE behaviour, so a failing
test proves the lead (they failed before the fixes)."""

import asyncio

import pytest
from conftest import TEST_ACCOUNTS, TEST_PASSWORD, install_test_users, login_as
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans, upload_flow
from reimburse_web.extract import Draft

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

PDF = b"%PDF-1.4\n%%EOF\n"
TRIP = dict(purpose="Visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")
RATE_LIMITED = "Terlalu banyak percobaan"


@pytest.fixture
def store():
    return install_test_users()


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("REIMBURSE_DEV_EMPLOYEE", raising=False)
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path / "uploads")
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    auth.reset_attempts()
    plans.STATE.clear()
    web.reset_all()


def _post_login(client, email, password):
    return client.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False)


def _denied(r):
    return r.status_code in (401, 403) or (r.status_code in (302, 303) and "/login" in r.headers["location"])


def test_own_successful_login_does_not_reset_the_throttle(client):
    victim = TEST_ACCOUNTS["EMP-102"][0]
    attacker = TEST_ACCOUNTS["EMP-001"][0]
    refused = None
    for n in range(1, 13):
        r = _post_login(client, victim, "wrong-password")
        if RATE_LIMITED in r.text:
            refused = n
            break
        assert r.status_code == 401
        if n % 4 == 0:  # the attacker's own success must not buy a fresh allowance (or be let through a full bucket)
            assert _post_login(client, attacker, TEST_PASSWORD).status_code == 303
    # 12 wrong guesses at the victim in under a minute: the 6th must already be refused.
    assert refused == 6


def test_deactivated_user_cookie_is_rejected(client, store):
    assert login_as(client, "EMP-102", "/admin").status_code == 303
    assert client.get("/admin", follow_redirects=False).status_code == 200
    user = store.find_by_employee_id("EMP-102")
    off = type(user)(user.id, user.employee_id, user.email, user.password_hash, user.role, False)
    store._users[user.email] = off
    store._by_employee[user.employee_id] = off
    assert _denied(client.get("/admin", follow_redirects=False))


def test_cookie_replay_after_logout_is_rejected(client):
    assert login_as(client, "EMP-102", "/admin").status_code == 303
    saved = client.cookies.get(auth.COOKIE)
    assert saved
    assert client.post("/logout", follow_redirects=False).status_code in (302, 303)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, saved)
    assert _denied(client.get("/admin", follow_redirects=False))


def test_extraction_is_offloaded_from_the_event_loop(client, monkeypatch):
    ran_on_loop = []

    class Stub:
        name = "stub"

        def extract(self, path):
            try:
                asyncio.get_running_loop()
                ran_on_loop.append(True)
            except RuntimeError:
                ran_on_loop.append(False)
            return Draft()

    monkeypatch.setattr(upload_flow, "get_extractor", lambda: Stub())
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    r = client.post("/employee/drafts", data=TRIP, files=[("files", ("a.pdf", PDF, "application/pdf"))],
                    follow_redirects=False)
    assert r.status_code == 303
    assert ran_on_loop == [False], "extract() ran on the event loop thread"


def test_rejected_draft_leaves_no_files_in_upload_dir(client, monkeypatch):
    class Stub:
        name = "stub"

        def extract(self, path):
            return Draft()

    monkeypatch.setattr(upload_flow, "get_extractor", lambda: Stub())
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    files = [("files", (f"a{i}.pdf", PDF, "application/pdf")) for i in range(3)]
    files.append(("files", ("big.pdf", b"0" * (web.MAX_UPLOAD_BYTES + 1), "application/pdf")))
    r = client.post("/employee/drafts", data=TRIP, files=files, follow_redirects=False)
    assert r.status_code == 413
    left = list(web.UPLOAD_DIR.glob("*")) if web.UPLOAD_DIR.exists() else []
    assert left == []
