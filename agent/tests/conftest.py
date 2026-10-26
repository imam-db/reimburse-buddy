import os
import pytest

# CI and local tests must never use a real model key.
@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for k in list(os.environ):
        if k.startswith("REIMBURSE_AGENT_") or k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)


# Audit: every graph built during the session writes to one JSONL log (REIMBURSE_AUDIT_LOG, set here unless
# the caller set one, so repeated runs can share a file). At the end, no execution may lack a matching
# approval (same run_id, action_id and args_hash).
def pytest_sessionstart(session):
    if not os.environ.get("REIMBURSE_AUDIT_LOG"):
        import tempfile
        os.environ["REIMBURSE_AUDIT_LOG"] = os.path.join(tempfile.mkdtemp(prefix="audit-"), "audit.jsonl")


def pytest_sessionfinish(session, exitstatus):
    from reimburse_agent.audit import JsonlAuditLog, unmatched_executions
    path = os.environ.get("REIMBURSE_AUDIT_LOG")
    if not path or not os.path.exists(path):
        return
    events = JsonlAuditLog(path).list()
    bad = unmatched_executions(events)
    n = sum(e["event_type"] == "execution" for e in events)
    print(f"\naudit: {len(events)} events, {n} executions, {len(bad)} without a matching approval ({path})")
    if bad:
        session.exitstatus = 1
