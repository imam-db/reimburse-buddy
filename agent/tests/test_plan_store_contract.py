"""PlanStore and MockOffice contract: the file backend must behave like the Postgres one.

Both are parametrized the same way as test_audit_contract.py: the postgres variant runs only with
`-m postgres` and DATABASE_URL set (the app role), and is skipped otherwise. Rows are written under a unique
run id and never removed — migration 006 gives reimburse_app SELECT, INSERT and UPDATE only, and a test that
needed DELETE would be testing a privilege the app does not have.
"""

import os
import uuid

import pytest

from reimburse_agent.office.directory import Directory
from reimburse_agent.office.store import FilePlanStore, PostgresPlanStore, make_pool
from reimburse_agent.office.tools import MockOffice, PostgresOfficeRows


def _dsn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set")
    return dsn


def rid(prefix: str = "test-plan") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(params=["file", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request, tmp_path):
    """A factory: calling it builds a fresh store on the same state, which is what a restart does."""
    if request.param == "file":
        yield lambda: FilePlanStore(tmp_path)
        return
    pool = make_pool(_dsn())
    yield lambda: PostgresPlanStore(pool)
    pool.close()


@pytest.fixture(params=["file", pytest.param("postgres", marks=pytest.mark.postgres)])
def office(request, tmp_path):
    directory = Directory.load()
    if request.param == "file":
        yield MockOffice(directory, tmp_path / "mock_office.json")
        return
    pool = make_pool(_dsn())
    yield MockOffice(directory, None, run_id=rid("test-office"), rows=PostgresOfficeRows(pool))
    pool.close()


# --- PlanStore -----------------------------------------------------------------------------------
def test_plan_roundtrip_and_the_newest_version_wins(backend):
    store, run = backend(), rid()
    store.save_plan({"run_id": run, "version": 1, "plan_id": f"{run}:v1", "actions": []})
    assert store.load_plan(run)["plan_id"] == f"{run}:v1"

    store.save_plan({"run_id": run, "version": 2, "plan_id": f"{run}:v2", "actions": []})
    # A re-plan adds a version; the page and the gate work on the newest one.
    assert store.load_plan(run)["plan_id"] == f"{run}:v2"
    assert store.load_plan(run)["version"] == 2


def test_saving_the_same_version_updates_it(backend):
    """The web layer stamps submitted_by on the plan it just read: same version, new field."""
    store, run = backend(), rid()
    store.save_plan({"run_id": run, "version": 1, "plan_id": f"{run}:v1", "actions": []})
    plan = store.load_plan(run)
    plan["submitted_by"] = "EMP-100"
    store.save_plan(plan)
    assert store.load_plan(run)["submitted_by"] == "EMP-100"
    assert store.load_plan(run)["version"] == 1


def test_decisions_roundtrip_per_version(backend):
    store, run = backend(), rid()
    assert store.load_decisions(run, 1) is None
    decision = {"plan_id": f"{run}:v1", "decisions": {"a1": "approve"}, "notes": {}, "actor": "human:EMP-102"}
    store.save_decisions(run, 1, decision)
    assert store.load_decisions(run, 1) == decision
    assert store.load_decisions(run, 2) is None  # a new version has no decision yet


def test_request_roundtrip_and_runs(backend):
    store, run = backend(), rid()
    request = {"request": "Kirim notulen ke Bu Sari.", "requester": "EMP-101", "submitted_by": "EMP-100",
               "created_at": "2026-10-26T02:00:00Z"}
    store.save_request(run, request)
    assert store.load_request(run) == request
    # runs() lists runs that have a plan (a run still on a who-question has none yet).
    assert run not in store.runs()
    store.save_plan({"run_id": run, "version": 1, "plan_id": f"{run}:v1", "actions": []})
    assert run in store.runs()


def test_unknown_run_is_none_everywhere(backend):
    store = backend()
    missing = rid("test-missing")
    assert store.load_plan(missing) is None
    assert store.load_request(missing) is None
    assert store.load_decisions(missing, 1) is None


def test_state_survives_a_new_store(backend):
    """Nothing is held in the object: a new process reads the same plan, decision and request."""
    run = rid()
    backend().save_plan({"run_id": run, "version": 1, "plan_id": f"{run}:v1", "actions": []})
    backend().save_request(run, {"request": "x", "requester": "EMP-101"})
    backend().save_decisions(run, 1, {"decisions": {"a1": "approve"}})
    fresh = backend()
    assert fresh.load_plan(run)["plan_id"] == f"{run}:v1"
    assert fresh.load_request(run)["requester"] == "EMP-101"
    assert fresh.load_decisions(run, 1) == {"decisions": {"a1": "approve"}}


# --- MockOffice ----------------------------------------------------------------------------------
def test_send_email_shape_and_one_row_per_action_id(office):
    first = office.send_email("R1:v1:a1", ["EMP-202"], "Notulen", "isi")
    assert first == {"id": "OUT-0001", "action_id": "R1:v1:a1", "to": ["EMP-202"],
                     "to_emails": ["budi.hartono@nusantara-niaga.example"], "subject": "Notulen", "body": "isi"}

    again = office.send_email("R1:v1:a1", ["EMP-202"], "Notulen", "isi")
    assert again["id"] == "OUT-0001" and again["duplicate"] is True
    assert office.count() == 1


def test_each_store_numbers_its_own_rows(office):
    assert office.send_email("R1:v1:a1", ["EMP-202"], "a", "b")["id"] == "OUT-0001"
    assert office.create_calendar_event("R1:v1:a2", ["EMP-101"], "t", "2026-10-30T06:00:00Z", 30)["id"] == "CAL-0001"
    assert office.create_task("R1:v1:a3", "EMP-101", "t")["id"] == "TSK-0001"
    assert office.send_email("R1:v1:a4", ["EMP-202"], "a", "b")["id"] == "OUT-0002"
    assert office.count() == 4


def test_a_replayed_action_keeps_the_first_row(office):
    """A resumed run replays an approved action: the store answers with the row it already has."""
    first = office.create_calendar_event("R1:v1:a1", ["EMP-101", "EMP-202"], "Follow-up", "2026-10-30T06:00:00Z", 30)
    again = office.create_calendar_event("R1:v1:a1", ["EMP-101", "EMP-202"], "Follow-up", "2026-10-30T06:00:00Z", 30)
    assert again["duplicate"] is True
    assert {k: v for k, v in again.items() if k != "duplicate"} == first
    assert office.count() == 1
