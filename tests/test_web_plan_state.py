"""The plan-state backend: which one is chosen, and what makes startup fail.

Production defaults to Postgres (`APP_ENV=prod`), everything else to the file store; an explicit
`REIMBURSE_PLAN_BACKEND` wins either way. With the postgres backend a missing DSN, an unreachable database
or a missing table aborts startup with a message that says what to run — a silent fall back to the file store
would lose the state this backend exists for.

No database is opened here: `plan_state.pool` is replaced by a fake that fails on the statement a test names.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import install_test_users, login_as
from reimburse_web import app as web
from reimburse_web import plan_state, plans

from reimburse_agent.office.store import FilePlanStore, StoreConfigError


class FakeCursor:
    def __init__(self, fail):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.fail(sql):
            raise RuntimeError("no such table")

    def fetchone(self):
        return (1,)


class FakeConnection:
    def __init__(self, fail):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.fail)


class FakePool:
    """A pool that answers `SELECT 1` and fails on whatever `fail` names."""

    def __init__(self, fail=lambda sql: False, down=False):
        self.fail = fail
        self.down = down
        self.closed = False

    def connection(self):
        if self.down:
            raise OSError("connection refused")
        return FakeConnection(self.fail)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("APP_ENV", "REIMBURSE_PLAN_BACKEND", "REIMBURSE_STATE_DATABASE_URL",
                 "REIMBURSE_AUDIT_DATABASE_URL", "REIMBURSE_AUDIT_LOG"):
        monkeypatch.delenv(name, raising=False)
    plan_state.reset()
    plans.STATE.clear()
    yield
    plan_state.reset()
    plans.STATE.clear()


def use_pool(monkeypatch, pool) -> None:
    monkeypatch.setenv("REIMBURSE_STATE_DATABASE_URL", "postgresql://app@db.example:5432/reimburse")
    monkeypatch.setattr(plan_state, "pool", lambda: pool)


# --- which backend -----------------------------------------------------------------------------
def test_file_backend_outside_production():
    assert plan_state.is_postgres() is False
    assert isinstance(plan_state.store(), FilePlanStore)
    plan_state.startup_check()  # nothing to check, no database involved


def test_production_defaults_to_postgres(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    assert plan_state.is_postgres() is True


def test_an_explicit_backend_wins(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "file")
    assert plan_state.is_postgres() is False
    plan_state.startup_check()


def test_an_unknown_backend_is_refused(monkeypatch):
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "sqlite")
    with pytest.raises(StoreConfigError) as exc:
        plan_state.store()
    assert "sqlite" in str(exc.value)


# --- what makes startup fail --------------------------------------------------------------------
def test_startup_fails_in_prod_without_a_dsn(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(StoreConfigError) as exc:
        plan_state.startup_check()
    assert "REIMBURSE_STATE_DATABASE_URL" in str(exc.value)


def test_the_audit_dsn_is_the_fallback(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://app@db.example:5432/reimburse")
    monkeypatch.setattr(plan_state, "pool", lambda: FakePool())
    plan_state.startup_check()  # no raise: one database serves audit, spend and plan state


def test_startup_fails_when_the_database_is_unreachable(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    use_pool(monkeypatch, FakePool(down=True))
    with pytest.raises(StoreConfigError) as exc:
        plan_state.startup_check()
    assert "unreachable" in str(exc.value)


def test_startup_fails_when_a_plan_table_is_missing(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    use_pool(monkeypatch, FakePool(fail=lambda sql: "plan_decisions" in sql))
    with pytest.raises(StoreConfigError) as exc:
        plan_state.startup_check()
    assert "plan_decisions" in str(exc.value) and "006_plan_state.sql" in str(exc.value)


def test_startup_fails_when_the_checkpoint_tables_are_missing(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    use_pool(monkeypatch, FakePool(fail=lambda sql: "checkpoint" in sql))
    with pytest.raises(StoreConfigError) as exc:
        plan_state.startup_check()
    assert "checkpoint" in str(exc.value) and "checkpoint_setup" in str(exc.value)


def test_the_web_app_refuses_to_start_with_a_broken_plan_state(monkeypatch):
    """The hook is wired into the app, not only callable: a real startup raises."""
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "postgres")
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "2.00")
    monkeypatch.setenv("LLM_PRICE_INPUT_MISS_PER_MTOK", "0.27")
    monkeypatch.setenv("LLM_PRICE_INPUT_HIT_PER_MTOK", "0.07")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "1.10")
    monkeypatch.setattr(plan_state, "pool", lambda: FakePool(down=True))

    from reimburse_agent import audit as A

    class FakePostgresLog:
        def __init__(self, dsn):
            pass

        def append(self, event):
            return True

        def list(self, run_id=None):
            return []

    monkeypatch.setattr(A, "PostgresAuditLog", FakePostgresLog)
    monkeypatch.setattr(A, "_PROD_LOG", None)

    with pytest.raises(StoreConfigError):
        with TestClient(web.app):
            pass


# --- startup order -----------------------------------------------------------------------------
def test_startup_runs_the_plan_state_check_before_the_sweep():
    modules = [hook.__module__ for hook in web.app.router.on_startup]
    assert modules == ["reimburse_web.plans", "reimburse_web.auth", "reimburse_web.spend",
                       "reimburse_web.plan_state", "reimburse_web.plans"]
