"""A request that is waiting survives a web restart.

Two things have to outlive the process: the plan graph's checkpoint (the open who-question, the pending
approval) and the plan state (request, plan, decisions, mock office rows). With
`REIMBURSE_PLAN_BACKEND=postgres` both live in the database, so `restart()` below — no `_LIVE`, no compiled
graph, no pool, no store object — is exactly what a deploy does to the process.

The Postgres test runs only with `-m postgres` and `DATABASE_URL` set (the app role, after
`agent/migrations/006_plan_state.sql` and `python -m reimburse_agent.office.checkpoint_setup`). The last test
uses the file backend, where a restart does lose the run: the page then has to say so instead of claiming the
decisions were saved. No LLM or paid API is touched.
"""

import os
import re

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, intake, plan_state, plans

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

from conftest import TEST_PASSWORD as PW, install_test_users, login_as
REQ = "Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up Jumat siang."
STALE = "tidak bisa dilanjutkan"


def _isolate(monkeypatch, tmp_path):
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    for name in ("APP_ENV", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    web.reset_all()
    plans.STATE.clear()
    intake.reset()
    plan_state.reset()
    auth.reset_attempts()


def _teardown():
    web.reset_all()
    plans.STATE.clear()
    intake.reset()
    plan_state.reset()
    auth.reset_attempts()


@pytest.fixture
def pg(tmp_path, monkeypatch):
    """The app on the Postgres backend (plan state and checkpoints in DATABASE_URL)."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set")
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "postgres")
    monkeypatch.setenv("REIMBURSE_STATE_DATABASE_URL", dsn)
    yield TestClient(web.app)
    _teardown()


@pytest.fixture
def file_backend(tmp_path, monkeypatch):
    """The app on the file backend: MemorySaver plus a directory of JSON files."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "file")
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    yield TestClient(web.app)
    _teardown()


def restart() -> None:
    """What a new process has: no live graph, no compiled graph, no pool, no cached store or log."""
    intake.reset()
    plan_state.reset()
    plans.STATE.pop("store", None)
    plans.STATE.pop("log", None)


def login(c, reviewer):
    r = login_as(c, reviewer)
    assert r.status_code == 303, r.text


def submit(c, request: str = REQ, requester: str = "EMP-101") -> str:
    r = c.post("/admin/intake", data={"request": request, "document": "", "requester": requester},
               follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert re.fullmatch(r"/admin/plans/req-[0-9a-f]{8}", location), location
    return location.rsplit("/", 1)[1]


def answer(c, run_id: str, emp: str = "EMP-202"):
    return c.post(f"/admin/plans/{run_id}/answer", data={"answer": emp}, follow_redirects=False)


def approve_form(run_id: str) -> dict:
    plan = plans.store().load_plan(run_id)
    data = {"plan_id": plan["plan_id"]}
    for action in plan["actions"]:
        data[f"decision:{action['action_id']}"] = "approve"
    return data


def events(run_id: str) -> list[dict]:
    return plans.log().list(run_id)


def office_rows(run_id: str, store: str) -> list[dict]:
    """What the mock office wrote for this run, straight from the shared database."""
    with plan_state.pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT row FROM mock_office_rows WHERE run_id = %s AND store = %s ORDER BY row->>'id'",
                    (run_id, store))
        return [row[0] for row in cur.fetchall()]


# --- Postgres: the restart is invisible to the reviewer -------------------------------------------
@pytest.mark.postgres
def test_example_1_question_answer_and_approval_survive_a_restart(pg):
    c = pg
    login(c, "EMP-100")
    run_id = submit(c)  # requester EMP-101, submitted by EMP-100

    page = c.get(f"/admin/plans/{run_id}")
    assert "Ada 2 karyawan bernama Budi" in page.text
    assert "Setujui / Approve" not in page.text

    restart()

    # The open question is still there: it was never in the process, only in the checkpointer.
    page = c.get(f"/admin/plans/{run_id}")
    assert page.status_code == 200
    assert "Ada 2 karyawan bernama Budi" in page.text
    assert "EMP-201" in page.text and "EMP-202" in page.text
    assert "Setujui / Approve" not in page.text

    r = answer(c, run_id)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/plans/{run_id}"
    plan = plans.store().load_plan(run_id)
    assert [a["tool"] for a in plan["actions"]] == ["send_email", "create_calendar_event"]
    assert not [e for e in events(run_id) if e["event_type"] == "execution"]

    restart()

    # The pending approval survived too, and the third reviewer may approve it.
    login(c, "EMP-102")
    page = c.get(f"/admin/plans/{run_id}")
    assert "Setujui / Approve" in page.text
    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(run_id), follow_redirects=False)
    assert r.status_code == 303
    assert "stale" not in r.headers["location"]

    outbox = office_rows(run_id, "outbox")
    calendar = office_rows(run_id, "calendar")
    assert len(outbox) == 1 and outbox[0]["to"] == ["EMP-202"]
    assert len(calendar) == 1 and calendar[0]["attendees"] == ["EMP-101", "EMP-202"]
    executions = [e for e in events(run_id) if e["event_type"] == "execution"]
    assert len(executions) == 2  # exactly one execution per approved action
    assert {e["actor"] for e in events(run_id) if e["event_type"] == "approval"} == {"human:EMP-102"}

    # A second submit of the same decisions cannot send the email again.
    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(run_id), follow_redirects=False)
    assert r.status_code == 303
    assert len(office_rows(run_id, "outbox")) == 1
    assert len([e for e in events(run_id) if e["event_type"] == "execution"]) == 2

    # The submitter is still the person who filled the form before the restart.
    assert plans.store().load_request(run_id)["submitted_by"] == "EMP-100"
    assert plans.store().load_plan(run_id)["submitted_by"] == "EMP-100"


@pytest.mark.postgres
def test_a_pending_approval_is_listed_after_a_restart(pg):
    c = pg
    login(c, "EMP-100")
    run_id = submit(c, "Kirim notulen ke Bu Sari ya, terus jadwalin follow-up Kamis pagi.")
    assert plans.store().load_plan(run_id)["actions"]  # one clear recipient: staged at once
    restart()
    login(c, "EMP-102")
    page = c.get("/admin/plans")
    assert run_id in page.text


# --- File backend: a lost thread must not look like a saved decision ------------------------------
def test_a_plan_whose_thread_is_gone_says_so_instead_of_saved(file_backend):
    c = file_backend
    login(c, "EMP-100")
    run_id = submit(c)
    assert "Ada 2 karyawan bernama Budi" in c.get(f"/admin/plans/{run_id}").text

    restart()  # MemorySaver went with the graph: the question is gone

    assert c.get(f"/admin/plans/{run_id}").status_code == 404  # no plan and no question left to show
    r = answer(c, run_id)
    assert r.status_code == 409
    assert STALE in r.text and "Jalankan ulang" in r.text
    assert f"action=\"/admin/intake/rerun/{run_id}\"" in r.text

    # The approval path says the same thing, and nothing runs.
    run_id = submit(c, "Kirim notulen ke Bu Sari.")  # one clear recipient: the plan is staged at once
    assert plans.store().load_plan(run_id)["actions"]
    restart()
    login(c, "EMP-102")  # EMP-100 submitted it, so somebody else has to approve
    r = c.post(f"/admin/plans/{run_id}/decide", data=approve_form(run_id), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?stale=1")

    page = c.get(f"/admin/plans/{run_id}?stale=1")
    assert page.status_code == 200
    assert STALE in page.text and "Jalankan ulang" in page.text
    assert "Keputusan disimpan" not in page.text
    assert "Setujui / Approve" not in page.text
    assert not [e for e in events(run_id) if e["event_type"] == "execution"]
    assert not (plans.store().root / run_id / "mock_office.json").exists()
