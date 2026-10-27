"""Production web startup refuses JSONL / in-memory audit logs.

APP_ENV=prod|production requires a connected Postgres audit log. The FastAPI startup hook raises
audit.AuditConfigError before the first request instead of silently falling back to JSONL or memory.
No real database is opened: the success case swaps PostgresAuditLog for a fake.
"""

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import plans

from reimburse_agent import audit as A
from reimburse_agent.office.store import PlanStore


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = PlanStore(tmp_path / "plans")
    monkeypatch.setattr(plans, "_SWEPT", False)
    monkeypatch.setattr(A, "_PROD_LOG", None)
    monkeypatch.setattr(A, "_DEFAULT", None)
    for name in ("APP_ENV", "REIMBURSE_AUDIT_LOG", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    yield
    plans.STATE.clear()


def test_plans_log_refuses_prod_without_database_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(A.AuditConfigError):
        plans.log()


def test_startup_fails_in_prod_without_database_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(A.AuditConfigError) as exc:
        with TestClient(web.app):
            pass

    assert "REIMBURSE_AUDIT_DATABASE_URL" in str(exc.value)


def test_startup_fails_in_prod_with_jsonl_only(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(path))

    with pytest.raises(A.AuditConfigError):
        with TestClient(web.app):
            pass

    assert not path.exists()


def test_startup_fails_in_prod_with_non_postgres_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "sqlite:///tmp/audit.db")

    with pytest.raises(A.AuditConfigError):
        with TestClient(web.app):
            pass


def test_startup_accepts_postgres_in_prod(monkeypatch):
    dsn = "postgresql://user@db.example:5432/reimburse"
    opened = []

    class FakePostgresLog:
        def __init__(self, got):
            opened.append(got)

        def append(self, event):
            return True

        def list(self, run_id=None):
            return []

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", dsn)
    # Production also requires the demo password gate (web/reimburse_web/auth.py) and the daily LLM
    # budget with its prices (web/reimburse_web/spend.py); without them startup stops with
    # AuthConfigError / SpendConfigError, which tests/test_web_auth.py and test_web_cost_cap.py cover.
    monkeypatch.setenv("DEMO_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "2.00")
    monkeypatch.setenv("LLM_PRICE_INPUT_MISS_PER_MTOK", "0.27")
    monkeypatch.setenv("LLM_PRICE_INPUT_HIT_PER_MTOK", "0.07")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "1.10")
    monkeypatch.setattr(A, "PostgresAuditLog", FakePostgresLog)

    with TestClient(web.app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303  # the demo password gate is on
        client.post("/login", data={"password": "correct-horse-battery-staple", "reviewer": "EMP-100"})
        assert client.get("/").status_code == 200

    assert opened == [dsn]
    assert isinstance(plans.log(), FakePostgresLog)


def test_startup_still_works_outside_prod_without_url():
    with TestClient(web.app) as client:
        assert client.get("/admin/plans").status_code == 200

    assert isinstance(plans.log(), A.JsonlAuditLog)
