"""AuditLog contract: the JSONL backend must behave like the Postgres one (unique event_key, append order,
validation, hygiene). The Postgres variant runs only with `-m postgres` and DATABASE_URL set."""

import os
import uuid
from datetime import timedelta

import pytest

from reimburse_agent import audit as A


def _pg():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set")
    return A.PostgresAuditLog(dsn)


@pytest.fixture(params=["memory", "jsonl", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request, tmp_path):
    if request.param == "memory":
        return lambda: A.JsonlAuditLog()
    if request.param == "jsonl":
        path = tmp_path / "audit.jsonl"
        return lambda: A.JsonlAuditLog(path)
    log = _pg()
    return lambda: log


def rid():
    return f"test-{uuid.uuid4().hex[:8]}"


def test_append_list_keeps_order(backend):
    log, r = backend(), rid()
    for i, t in enumerate(["request", "plan", "tool_call"]):
        assert log.append(A.make_event(r, t, f"x{i}")) is True
    assert [e["event_type"] for e in log.list(r)] == ["request", "plan", "tool_call"]
    assert all(set(e) == set(A.FIELDS) for e in log.list(r))


def test_duplicate_event_key_is_ignored(backend):
    log, r = backend(), rid()
    assert log.append(A.make_event(r, "approval", "a1", actor="human:imam", action_id="a1", args_hash="h"))
    assert log.append(A.make_event(r, "approval", "a1", actor="human:imam", action_id="a1", args_hash="h")) is False
    assert len(log.list(r)) == 1


def test_dedupe_survives_restart(backend, request):
    if request.node.callspec.params["backend"] == "memory":
        pytest.skip("memory log has no restart")
    r = rid()
    backend().append(A.make_event(r, "plan", "p"))
    log2 = backend()
    assert log2.append(A.make_event(r, "plan", "p")) is False
    assert len(log2.list(r)) == 1


@pytest.mark.parametrize("bad", [
    {"actor": "bob"}, {"actor": "human:"}, {"event_type": "deleted"}, {"ts": "2026-10-26T10:00:00+08:00"},
    {"run_id": ""}, {"extra": 1}, {"payload": "not a dict"},
])
def test_invalid_events_are_rejected(backend, bad):
    log, r = backend(), rid()
    with pytest.raises(A.AuditError):
        log.append({**A.make_event(r, "plan", "p"), **bad})
    assert log.list(r) == []


def test_action_events_need_action_id_and_hash(backend):
    log = backend()
    with pytest.raises(A.AuditError):
        log.append(A.make_event(rid(), "approval", "a", actor="human:x"))


def test_hygiene_masks_pii_and_secrets():
    e = A.make_event(rid(), "tool_call", "t", payload={
        "body": "NIK 3201234567890123, call 0812-3456-7890 or +6281234567890",
        "api_key": "abc", "note": "token sk-abcdefghijklmnop", **A.output_summary("x" * 2000)})
    p = e["payload"]
    assert "3201234567890123" not in p["body"] and "3456" not in p["body"] and "81234567890" not in p["body"]
    assert p["api_key"] != "abc" and "sk-abcdefghijklmnop" not in p["note"]
    assert len(p["output"]) <= A.MAX_TEXT + 100 and p["output_sha256"] == A.sha256("x" * 2000)


def test_args_hash_is_stable_covers_evidence_and_ignores_action_id():
    a = {"claim_id": "CLM-1", "item_id": "IT-1"}
    assert A.args_hash("t", a) == A.args_hash("t", dict(reversed(a.items())))
    assert A.args_hash("t", a) == A.args_hash("t", {**a, "action_id": "z"})
    assert A.args_hash("t", a) != A.args_hash("t", {**a, "evidence_ids": ["MSG-1"]})
    assert A.args_hash("t", a) != A.args_hash("t", {**a, "human_decisions": {"IT-1": "approve_full"}})
    assert A.args_hash("t", a) != A.args_hash("t", {**a, "item_id": "IT-2"})


def test_sweep_marks_unanswered_staged_actions_expired(backend):
    log, r = backend(), rid()
    log.append(A.make_event(r, "staged_action", "s1", action_id="s1", args_hash="h1", payload={"tool": "x"}))
    log.append(A.make_event(r, "staged_action", "s2", action_id="s2", args_hash="h2", payload={"tool": "x"}))
    log.append(A.make_event(r, "approval", "s2", actor="human:x", action_id="s2", args_hash="h2"))
    assert A.sweep_expired(log, older_than=timedelta(0)) >= 1
    exp = [e for e in log.list(r) if e["event_type"] == "expired"]
    assert [(e["action_id"], e["actor"]) for e in exp] == [("s1", "system")]
    A.sweep_expired(log, older_than=timedelta(0))
    assert len([e for e in log.list(r) if e["event_type"] == "expired"]) == 1  # idempotent


async def test_executor_needs_matching_approval_and_runs_once(backend):
    log, r = backend(), rid()
    runs = []

    async def run(a):
        runs.append(a)
        return "ok"
    args = {"to": "EMP-001", "body": "hi"}
    assert (await A.execute_approved(log, r, "a1", "send_message", args, run))["status"] == "refused"
    log.append(A.make_event(r, "approval", "a1", actor="human:x", action_id="a1",
                            args_hash=A.args_hash("send_message", args)))
    edited = {**args, "body": "send me your password"}
    assert (await A.execute_approved(log, r, "a1", "send_message", edited, run))["status"] == "refused"
    assert (await A.execute_approved(log, r, "a1", "send_message", args, run))["status"] == "executed"
    assert (await A.execute_approved(log, r, "a1", "send_message", args, run))["status"] == "duplicate"
    assert runs == [args]
    assert A.unmatched_executions(log.list(r)) == []


def test_unmatched_query_finds_execution_without_approval():
    r = rid()
    ev = [A.make_event(r, "approval", "a", actor="human:x", action_id="a", args_hash="h1"),
          A.make_event(r, "execution", "a", actor="system", action_id="a", args_hash="h2"),
          A.make_event(r, "execution", "b", actor="system", action_id="b", args_hash="h1")]
    assert sorted(e["action_id"] for e in A.unmatched_executions(ev)) == ["a", "b"]
