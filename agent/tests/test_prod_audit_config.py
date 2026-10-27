"""Production refuses JSONL / in-memory audit logs: APP_ENV=prod|production requires a connected
Postgres log, and default_log() plus the worker startup path raise a clear AuditConfigError otherwise.
Tests never open a real database: the success case swaps PostgresAuditLog for a fake.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reimburse_agent import audit as A
from reimburse_agent.audit import AuditConfigError, AuditError

AGENT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("APP_ENV", "REIMBURSE_AUDIT_LOG", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(A, "_PROD_LOG", None)
    monkeypatch.setattr(A, "_DEFAULT", None)


def test_audit_config_error_is_an_audit_error():
    assert issubclass(AuditConfigError, AuditError)


@pytest.mark.parametrize("app_env", ["prod", "PROD", "Production", " production "])
def test_prod_without_url_raises(app_env, monkeypatch):
    monkeypatch.setenv("APP_ENV", app_env)

    with pytest.raises(AuditConfigError) as exc:
        A.default_log()
    assert "REIMBURSE_AUDIT_DATABASE_URL" in str(exc.value)

    with pytest.raises(AuditConfigError):
        A.check_audit_config()


def test_prod_with_jsonl_only_raises_and_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(path))

    with pytest.raises(AuditConfigError):
        A.default_log()

    assert not path.exists()


def test_prod_with_non_postgres_url_raises_without_connecting(monkeypatch):
    opened = []
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "sqlite:///tmp/audit.db")
    monkeypatch.setattr(A, "PostgresAuditLog", lambda dsn: opened.append(dsn))

    with pytest.raises(AuditConfigError) as exc:
        A.default_log()

    assert "postgres" in str(exc.value).lower()
    assert opened == []


def test_prod_with_postgres_url_opens_the_log_once(monkeypatch):
    dsn = "postgresql://user@db.example:5432/reimburse"
    opened = []

    class FakePostgresLog:
        def __init__(self, got):
            opened.append(got)
            self.rows = []

        def append(self, event):
            self.rows.append(event)
            return True

        def list(self, run_id=None):
            return [e for e in self.rows if run_id is None or e["run_id"] == run_id]

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", dsn)
    monkeypatch.setattr(A, "PostgresAuditLog", FakePostgresLog)

    log = A.default_log()
    assert isinstance(log, FakePostgresLog)
    assert opened == [dsn]

    A.check_audit_config()
    assert A.default_log() is log
    assert opened == [dsn]


def test_prod_connection_failure_is_a_config_error(monkeypatch):
    def refuse(dsn):
        raise OSError("connection refused")

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgres://db.example/reimburse")
    monkeypatch.setattr(A, "PostgresAuditLog", refuse)

    with pytest.raises(AuditConfigError) as exc:
        A.check_audit_config()

    assert "cannot open" in str(exc.value)


def test_require_production_log_is_a_noop_outside_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example/reimburse")

    assert A.require_production_log() is None


def test_non_prod_without_url_still_uses_in_memory_log(monkeypatch):
    log = A.default_log()

    assert isinstance(log, A.JsonlAuditLog) and log.path is None
    assert log.append(A.make_event("run", "request", "start")) is True
    assert [e["event_type"] for e in log.list("run")] == ["request"]


def test_non_prod_with_jsonl_env_still_writes_jsonl(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(path))

    log = A.default_log()
    assert isinstance(log, A.JsonlAuditLog) and log.path == path
    assert log.append(A.make_event("run", "request", "start")) is True

    assert path.exists()
    assert [e["event_type"] for e in A.JsonlAuditLog(path).list("run")] == ["request"]


def test_worker_refuses_to_start_in_prod_without_postgres():
    env = dict(os.environ, APP_ENV="prod")
    for name in ("REIMBURSE_AUDIT_LOG", "REIMBURSE_AUDIT_DATABASE_URL",
                 "REIMBURSE_AGENT_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(name, None)

    proc = subprocess.run([sys.executable, "investigate_worker.py", "--claim", "CLM-2026-0110"],
                          cwd=AGENT, env=env, capture_output=True, text=True, timeout=120)

    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert proc.returncode == 6
    assert events == [{"type": "error", "code": "config", "message": events[0]["message"]}]
    assert "REIMBURSE_AUDIT_DATABASE_URL" in events[0]["message"]
