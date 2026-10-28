import json
import shutil
from pathlib import Path

import pytest

from reimburse_buddy.data_store import DEFAULT_DATA_DIR, DataStore

from reimburse_web import auth, users

# Login test accounts (test values only). Emails are the ones in data/employees.json.
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_ACCOUNTS = {  # employee_id -> (email, role)
    "EMP-100": ("dewi.anggraini@nusantara-niaga.example", "reviewer"),
    "EMP-101": ("hendra.saputra@nusantara-niaga.example", "reviewer"),
    "EMP-102": ("rani.kusuma@nusantara-niaga.example", "finance"),
    "EMP-001": ("rina.wulandari@nusantara-niaga.example", "employee"),
    "EMP-003": ("sari.kusumawati@nusantara-niaga.example", "employee"),
}
_HASH: dict[str, str] = {}


def install_test_users(roles: dict[str, str] | None = None) -> users.MemoryUserStore:
    """Install a MemoryUserStore with the accounts above (one scrypt hash, computed once per run).

    `roles` overrides the role per employee_id, e.g. {"EMP-101": "finance"} for the self-approval scene."""
    _HASH.setdefault("pw", users.hash_password(TEST_PASSWORD))
    store = users.MemoryUserStore()
    for emp, (email, role) in TEST_ACCOUNTS.items():
        store.add(emp, email, role=(roles or {}).get(emp, role), password_hash=_HASH["pw"])
    auth.set_store(store)
    return store


def login_as(client, employee_id: str, next_path: str = "/admin"):
    """Sign `client` in as one of TEST_ACCOUNTS and return the (non-followed) response."""
    client.cookies.clear()
    return client.post("/login", data={"email": TEST_ACCOUNTS[employee_id][0], "password": TEST_PASSWORD,
                                       "next": next_path}, follow_redirects=False)


@pytest.fixture(autouse=True)
def _clean_login_state(monkeypatch):
    """No test may reach a real user database or inherit another test's user store."""
    for name in ("REIMBURSE_AUTH_DATABASE_URL", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    auth.set_store(None)
    yield
    auth.set_store(None)


@pytest.fixture
def make_store(tmp_path):
    """Copy the synthetic data, apply edits, return a fresh DataStore."""
    def _make(claims=(), inbox=(), calendar=(), memos=()):
        d = tmp_path / "data"
        shutil.copytree(DEFAULT_DATA_DIR, d)
        for name, extra in (("claims", claims), ("inbox", inbox), ("calendar", calendar), ("memos", memos)):
            rows = json.loads((d / f"{name}.json").read_text())
            rows.extend(extra)
            (d / f"{name}.json").write_text(json.dumps(rows))
        return DataStore(d)
    return _make


def claim(cid, items, emp="EMP-001", dest="Surabaya", start="2026-10-12", end="2026-10-14"):
    return {"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": end, "purpose": "test",
            "trip": {"origin": "Jakarta", "destination": dest, "start_date": start, "end_date": end},
            "items": items}


# Agent end-to-end tests spawn the agent's own uv env (slow, needs uv + agent/uv.lock).
# Opt in with `pytest -m agent` or RB_AGENT_TESTS=1; the default run stays fast and offline.
def pytest_configure(config):
    config.addinivalue_line("markers", "agent: web <-> agent subprocess tests (opt-in: -m agent)")


def pytest_collection_modifyitems(config, items):
    import os

    if os.environ.get("RB_AGENT_TESTS") == "1" or "agent" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="opt-in: run with -m agent or RB_AGENT_TESTS=1")
    for item in items:
        if "agent" in item.keywords:
            item.add_marker(skip)
