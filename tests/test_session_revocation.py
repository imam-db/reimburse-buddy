"""Logout survives a restart when the user store keeps revocations (agent/migrations/009_revoked_sessions.sql).

The in-process list in auth.py is lost when the web process restarts; the store-backed one is not. Without
table 009 the Postgres store reports revocations as unavailable and the app keeps working in memory only.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import install_test_users, login_as
from reimburse_web import app as web
from reimburse_web import auth, users


@pytest.fixture
def store():
    return install_test_users()


@pytest.fixture
def client(monkeypatch, store, tmp_path):
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret-0123456789")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("APP_ENV", raising=False)
    web.reset_all()
    auth.reset_attempts()
    yield TestClient(web.app)
    auth.reset_attempts()
    web.reset_all()


def test_logout_is_stored_and_survives_a_restart(client, store):
    assert login_as(client, "EMP-102", "/admin").status_code == 303
    saved = client.cookies.get(auth.COOKIE)
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert len(store.revoked) == 1  # written to the store, not only to memory

    auth._REVOKED.clear()  # what a restart does to the in-process list
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, saved)
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_other_sessions_of_the_same_user_stay_valid(client):
    assert login_as(client, "EMP-102", "/admin").status_code == 303
    first = client.cookies.get(auth.COOKIE)
    client.cookies.clear()
    assert login_as(client, "EMP-102", "/admin").status_code == 303
    assert client.post("/logout", follow_redirects=False).status_code == 303  # logs out the second only
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, first)
    assert client.get("/admin", follow_redirects=False).status_code == 200


class _MissingTable(Exception):
    sqlstate = "42P01"


def test_postgres_store_without_table_009_falls_back(monkeypatch):
    """No 009 yet: the session query degrades to the user row alone, revoke reports False, no error."""
    s = users.PostgresUserStore.__new__(users.PostgresUserStore)
    s.revocations_available = True
    calls = []

    def run(sql, params, fetch):
        calls.append(sql)
        if "revoked_sessions" in sql:
            raise _MissingTable()
        return (1, "EMP-102", "a@b.c", "h", "finance", True) if fetch else None

    s._run = run
    user, revoked = s.session_state("EMP-102", "nonce")
    assert user.employee_id == "EMP-102" and revoked is False
    assert s.revocations_available is False
    assert s.revoke_session("nonce", 9e9) is False
    n = len(calls)
    s.session_state("EMP-102", "nonce")  # no further attempts at the missing table
    assert not any("revoked_sessions" in c for c in calls[n:])


def test_postgres_store_reraises_other_errors(monkeypatch):
    s = users.PostgresUserStore.__new__(users.PostgresUserStore)
    s.revocations_available = True

    def run(sql, params, fetch):
        raise RuntimeError("connection lost")

    s._run = run
    with pytest.raises(RuntimeError):
        s.session_state("EMP-102", "nonce")
