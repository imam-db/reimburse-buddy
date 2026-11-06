"""The demo profile picker (docs/adr/0012-demo-profiles.md): stage 1, the switch and the user store.

`REIMBURSE_DEMO_PROFILES=1` is on only outside production and only with the login gate on; production refuses
to start when it is set at all. The accounts a card may stand for are `auth.DEMO_PROFILES` in order, and only
the rows that exist and are active in `app_users` (here a MemoryUserStore: no database is touched).
"""

import pytest
from conftest import TEST_ACCOUNTS, install_test_users
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans, users

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

DEMO_ON = "1"


@pytest.fixture
def store():
    return install_test_users()


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.delenv("APP_ENV", raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    auth.reset_attempts()
    plans.STATE.clear()


# --- the switch ---------------------------------------------------------------------------
def test_the_switch_is_on_with_the_flag_outside_production_and_the_gate_on(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    assert auth.demo_profiles_enabled()


def test_the_switch_is_off_without_the_flag(client, monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    assert not auth.demo_profiles_enabled()
    # Any value other than 1 leaves it off: a typo can never open a passwordless way in.
    for value in ("", "  ", "0", "true", "yes", "2"):
        monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", value)
        assert not auth.demo_profiles_enabled(), value


def test_the_switch_is_off_in_production_even_with_the_flag(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    monkeypatch.setenv("APP_ENV", "prod")
    assert not auth.demo_profiles_enabled()
    monkeypatch.setenv("APP_ENV", "production")
    assert not auth.demo_profiles_enabled()


def test_the_switch_is_off_when_the_login_gate_is_off(tmp_path, monkeypatch):
    """With the gate off there is no sign-in at all, so there is nothing for the picker to do."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    auth.set_store(None)
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog()
    assert not auth.enabled()
    assert not auth.demo_profiles_enabled()
    assert auth.demo_profile_users() == []
    assert "Gerbang login mati" in TestClient(web.app).get("/login").text


# --- production refuses to start -----------------------------------------------------------
@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "file")
    yield monkeypatch
    plans.STATE.clear()


def test_production_with_the_switch_refuses_to_start_and_names_the_variable(prod):
    for value in ("1", "true", "0", "anything"):
        prod.setenv("REIMBURSE_DEMO_PROFILES", value)
        with pytest.raises(auth.AuthConfigError) as exc:
            auth.startup_check()
        assert "REIMBURSE_DEMO_PROFILES" in str(exc.value), value


def test_production_without_the_switch_starts(prod, store):
    prod.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    auth.startup_check()  # no exception


def test_the_startup_refusal_survives_the_other_checks(prod):
    """The switch is refused even when the rest of the production config is incomplete: no ordering escape."""
    prod.delenv("REIMBURSE_DEMO_SESSION_KEY", raising=False)
    prod.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.startup_check()
    assert "REIMBURSE_DEMO_PROFILES" in str(exc.value)


# --- which profiles -----------------------------------------------------------------------
def test_the_profiles_are_the_five_demo_accounts_in_order():
    assert auth.DEMO_PROFILES == ("EMP-001", "EMP-003", "EMP-100", "EMP-101", "EMP-102")


def test_the_card_list_follows_the_demo_order_and_the_row_role(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    rows = auth.demo_profile_users()
    assert [u.employee_id for u in rows] == list(auth.DEMO_PROFILES)
    assert [u.role for u in rows] == ["employee", "employee", "reviewer", "reviewer", "finance"]


def test_the_role_comes_from_the_row_not_from_the_list(client, monkeypatch):
    """Re-seeding EMP-101 as finance moves that card's role; nothing in the picker decides it."""
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    install_test_users(roles={"EMP-101": "finance"})
    roles = {u.employee_id: u.role for u in auth.demo_profile_users()}
    assert roles["EMP-101"] == "finance"


def test_an_inactive_or_missing_account_gets_no_card(client, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    store.add("EMP-003", "sari.kusumawati@nusantara-niaga.example", "a-long-enough-secret", "employee",
              is_active=False)  # the later row wins the employee_id index, and it is inactive
    listed = [u.employee_id for u in auth.demo_profile_users()]
    assert "EMP-003" not in listed
    assert listed == ["EMP-001", "EMP-100", "EMP-101", "EMP-102"]

    fresh = users.MemoryUserStore()
    for emp in ("EMP-001", "EMP-100"):  # EMP-003, EMP-101 and EMP-102 were never created
        fresh.add(emp, TEST_ACCOUNTS[emp][0], role=TEST_ACCOUNTS[emp][1], password_hash="scrypt$1$1$1$00$00")
    auth.set_store(fresh)
    assert [u.employee_id for u in auth.demo_profile_users()] == ["EMP-001", "EMP-100"]


def test_the_card_list_is_empty_when_the_switch_is_off(client, monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    assert auth.demo_profile_users() == []


def test_a_broken_user_store_is_an_empty_list_not_a_crash(client, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)

    def boom(employee_id):
        raise RuntimeError("db down: postgresql://secret")

    monkeypatch.setattr(store, "find_by_employee_id", boom)
    assert auth.demo_profile_users() == []


# --- find_by_employee_id ------------------------------------------------------------------
def test_memory_store_finds_a_row_by_employee_id():
    store = users.MemoryUserStore()
    store.add("EMP-100", "Dewi@Example.com", "a-long-enough-secret", "reviewer")
    found = store.find_by_employee_id("EMP-100")
    assert found is not None and found.employee_id == "EMP-100" and found.role == "reviewer"
    assert store.find_by_employee_id(" EMP-100 ") is not None  # the form value is trimmed
    assert store.find_by_employee_id("EMP-999") is None
    assert store.find_by_employee_id("") is None


def test_memory_store_keeps_the_latest_row_for_one_employee_id():
    store = users.MemoryUserStore()
    store.add("EMP-100", "old@example.com", "a-long-enough-secret", "reviewer")
    store.add("EMP-100", "new@example.com", "a-long-enough-secret", "finance")
    assert store.find_by_employee_id("EMP-100").role == "finance"
