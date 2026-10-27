"""Expiry sweep placement: opening a log or constructing what an agent worker uses never expires a plan;
sweep_expired only expires old, unanswered, orphaned staged actions."""

from datetime import datetime, timedelta, timezone

from reimburse_agent import audit as A

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def staged(run_id: str, action_id: str = "a1", *, age_minutes: float = 0.0, ts: str | None = None) -> dict:
    event = A.make_event(run_id, "staged_action", action_id, action_id=action_id, args_hash="h",
                         payload={"tool": "send_email"})
    event["ts"] = ts or (NOW - timedelta(minutes=age_minutes)).isoformat(timespec="microseconds")
    return event


def test_default_log_does_not_sweep_a_pending_plan(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    A.JsonlAuditLog(path).append(staged("pending", age_minutes=120))
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(path))
    monkeypatch.setattr(A, "_DEFAULT", None)

    log = A.default_log()
    A.default_log()  # a second worker opening the same shared log

    assert [e["event_type"] for e in log.list("pending")] == ["staged_action"]
    assert [e["event_type"] for e in A.JsonlAuditLog(path).list("pending")] == ["staged_action"]


def test_sweep_expires_only_old_orphaned_actions():
    log = A.JsonlAuditLog()
    log.append(staged("orphan", age_minutes=45))
    log.append(staged("recent", age_minutes=5))
    log.append(staged("live", age_minutes=45))

    n = A.sweep_expired(log, older_than=timedelta(minutes=30), live={"live"}, now=NOW)

    assert n == 1
    expired = [e for e in log.list() if e["event_type"] == "expired"]
    assert [(e["run_id"], e["action_id"], e["actor"]) for e in expired] == [("orphan", "a1", "system")]
    assert expired[0]["payload"]["reason"] == A.EXPIRY_REASON_DEADLINE


def test_sweep_accepts_callable_live_set_and_is_idempotent():
    log = A.JsonlAuditLog()
    log.append(staged("orphan", age_minutes=45))
    log.append(staged("live", age_minutes=45))

    assert A.sweep_expired(log, older_than=timedelta(minutes=30), live=lambda: {"live"}, now=NOW) == 1
    assert A.sweep_expired(log, older_than=timedelta(minutes=30), live=lambda: {"live"}, now=NOW) == 0


def test_sweep_keeps_actions_that_already_have_an_outcome():
    log = A.JsonlAuditLog()
    log.append(staged("answered", age_minutes=45))
    log.append(A.make_event("answered", "approval", "a1", actor="human:reviewer", action_id="a1", args_hash="h"))

    assert A.sweep_expired(log, older_than=timedelta(minutes=30), live=set(), now=NOW) == 0
    assert not [e for e in log.list() if e["event_type"] == "expired"]


def test_deadline_comes_from_env(monkeypatch):
    monkeypatch.setenv("REIMBURSE_APPROVAL_DEADLINE_MIN", "5")
    log = A.JsonlAuditLog()
    log.append(staged("ten-minutes", age_minutes=10))

    assert A.approval_deadline() == timedelta(minutes=5)
    assert A.sweep_expired(log, live=set(), now=NOW) == 1
