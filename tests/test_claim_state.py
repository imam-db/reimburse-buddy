"""The claim-state store (ADR 0010): which backend is chosen, what startup refuses, and the id rule.

No database is opened here. `plan_state.pool` is replaced by a fake, and the store itself is a small
in-memory double, so these tests run in the default `file` backend exactly like the rest of the suite.
"""

import json
import pytest

from reimburse_web import claim_state, plan_state
from reimburse_web.claim_state import MemoryClaimRows

from reimburse_agent.office.store import StoreConfigError


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

    def connection(self):
        if self.down:
            raise OSError("connection refused")
        return FakeConnection(self.fail)


class RecordingRows(MemoryClaimRows):
    """Memory rows that remember every call, to prove a route uses the right function."""

    def __init__(self):
        self.calls = []

    def __getattribute__(self, name):
        if name in ("calls",) or name.startswith("_"):
            return object.__getattribute__(self, name)
        attr = object.__getattribute__(self, name)
        if not callable(attr) or name == "transaction":
            return attr

        def record(*args, **kwargs):
            object.__getattribute__(self, "calls").append((name, args, kwargs))
            return attr(*args, **kwargs)

        return record


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("APP_ENV", "REIMBURSE_PLAN_BACKEND", "REIMBURSE_STATE_DATABASE_URL",
                 "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    claim_state.reset()
    yield
    claim_state.reset()


def use_postgres(monkeypatch, pool):
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "postgres")
    monkeypatch.setenv("REIMBURSE_STATE_DATABASE_URL", "postgresql://app@db.example:5432/reimburse")
    monkeypatch.setattr(plan_state, "pool", lambda: pool)


# --- which backend ---------------------------------------------------------------------------
def test_memory_rows_is_the_default_outside_production():
    assert isinstance(claim_state.rows(), MemoryClaimRows)
    claim_state.startup_check()  # nothing to check, no database involved


def test_the_postgres_backend_is_used_when_asked(monkeypatch):
    use_postgres(monkeypatch, FakePool())
    assert isinstance(claim_state.rows(), claim_state.PostgresClaimRows)


def test_memory_rows_keep_no_state():
    rows = MemoryClaimRows()
    claim = {"id": "CLM-2026-W001", "employee_id": "EMP-001", "items": []}
    assert rows.add_claim(claim) == "CLM-2026-W001"
    assert rows.add_message({"id": "MSG-W001"}) == "MSG-W001"
    assert rows.add_outbox({"id": "OUT-001"}) == "OUT-001"
    assert rows.read_file("anything.png") is None
    rows.save_review("CLM-2026-W001", {})
    rows.save_draft({"id": "abc", "emp": "EMP-001"})
    rows.mark_draft_submitted("abc")
    rows.save_approval_request({"id": "APR-001"})
    rows.add_status_change({"claim_id": "CLM-2026-W001", "from": "submitted", "to": "approved", "note": ""})
    rows.save_file("x.png", b"bytes", "EMP-001")
    with rows.transaction():
        pass
    assert rows.bump_epoch() == 1
    rows.hydrate(None, {}, {})  # nothing to replay


def test_a_test_double_can_replace_the_store():
    rows = RecordingRows()
    claim_state.set_rows(rows)
    claim_state.add_claim({"id": "CLM-2026-W009", "employee_id": "EMP-001"})
    assert rows.calls and rows.calls[0][0] == "add_claim"


# --- what makes startup fail ------------------------------------------------------------------
def test_startup_fails_when_a_claim_table_is_missing(monkeypatch):
    use_postgres(monkeypatch, FakePool(fail=lambda sql: "claim_reviews" in sql))
    with pytest.raises(StoreConfigError) as exc:
        claim_state.startup_check()
    assert "claim_reviews" in str(exc.value)
    assert "008_claim_state.sql" in str(exc.value)


def test_startup_fails_when_the_database_is_unreachable(monkeypatch):
    use_postgres(monkeypatch, FakePool(down=True))
    with pytest.raises(StoreConfigError) as exc:
        claim_state.startup_check()
    assert "unreachable" in str(exc.value)
    assert "REIMBURSE_STATE_DATABASE_URL" in str(exc.value)


def test_startup_passes_when_every_table_is_there(monkeypatch):
    use_postgres(monkeypatch, FakePool())
    claim_state.startup_check()  # no raise


def test_startup_names_every_missing_table(monkeypatch):
    use_postgres(monkeypatch, FakePool(fail=lambda sql: "uploaded_files" in sql or "demo_epoch" in sql))
    with pytest.raises(StoreConfigError) as exc:
        claim_state.startup_check()
    assert "demo_epoch" in str(exc.value) and "uploaded_files" in str(exc.value)


def test_the_web_app_refuses_to_start_with_a_broken_claim_state(monkeypatch):
    """The hook is wired into the app, not only callable: a real startup raises."""
    from fastapi.testclient import TestClient

    from conftest import install_test_users
    from reimburse_web import app as web

    from reimburse_agent import audit as A

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "postgres")
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret-0123456789abcdef")
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "2.00")
    monkeypatch.setenv("LLM_PRICE_INPUT_MISS_PER_MTOK", "0.27")
    monkeypatch.setenv("LLM_PRICE_INPUT_HIT_PER_MTOK", "0.07")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "1.10")
    # The plan-state tables are fine; only the claim-state ones are missing.
    monkeypatch.setattr(plan_state, "pool", lambda: FakePool(fail=lambda sql: "web_claims" in sql))

    class FakePostgresLog:
        def __init__(self, dsn):
            pass

        def append(self, event):
            return True

        def list(self, run_id=None):
            return []

    monkeypatch.setattr(A, "PostgresAuditLog", FakePostgresLog)
    monkeypatch.setattr(A, "_PROD_LOG", None)

    with pytest.raises(StoreConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "web_claims" in str(exc.value) and "008_claim_state.sql" in str(exc.value)


# --- the id rule --------------------------------------------------------------------------------
@pytest.mark.parametrize("before, after", [
    ("CLM-2026-W001", "CLM-2026-W002"),
    ("MSG-W009", "MSG-W010"),
    ("OUT-001", "OUT-002"),
    ("APR-099", "APR-100"),
])
def test_the_next_number_keeps_the_padding(before, after):
    assert claim_state._bump(before) == after


def test_an_id_without_a_number_cannot_advance():
    with pytest.raises(ValueError):
        claim_state._bump("CLM-2026-W")


# --- file paths ----------------------------------------------------------------------------------
class FileRows(MemoryClaimRows):
    def __init__(self, content):
        self.content = content

    def read_file(self, stored_as):
        return self.content


def test_restore_file_returns_the_disk_copy_when_it_is_there(tmp_path):
    (tmp_path / "a.png").write_bytes(b"disk")
    claim_state.set_rows(FileRows(b"database"))
    assert claim_state.restore_file(tmp_path, "a.png").read_bytes() == b"disk"


def test_restore_file_brings_the_bytes_back_from_the_database(tmp_path):
    claim_state.set_rows(FileRows(b"database"))
    path = claim_state.restore_file(tmp_path, "gone.png")
    assert path is not None and path.read_bytes() == b"database"
    assert (tmp_path / "gone.png").is_file()


def test_restore_file_returns_none_when_the_file_is_nowhere(tmp_path):
    claim_state.set_rows(FileRows(None))
    assert claim_state.restore_file(tmp_path, "missing.png") is None


def test_restore_file_refuses_a_name_that_climbs_out_of_the_upload_dir(tmp_path):
    claim_state.set_rows(FileRows(b"secret"))
    assert claim_state.restore_file(tmp_path / "uploads", "../escape.png") is None


def test_a_moved_id_is_also_the_id_inside_the_stored_json():
    """On a key collision the row takes the next number; its JSON must say the same number, or a replay
    brings back the old one (found against a real Neon branch)."""
    from contextlib import contextmanager

    taken = {"CLM-2026-W001", "CLM-2026-W002"}
    stored: dict[str, dict] = {}

    class Cursor:
        def execute(self, sql, params):
            *_, ident, payload = params
            self.row = None if ident in taken else (ident,)
            if self.row:
                stored[ident] = json.loads(payload)

        def fetchone(self):
            return self.row

    rows = claim_state.PostgresClaimRows(pool=None)
    rows._epoch = 1
    rows._cursor = contextmanager(lambda: (yield Cursor()))
    claim = {"id": "CLM-2026-W001", "employee_id": "EMP-001", "items": []}

    assert rows.add_claim(claim) == "CLM-2026-W003"
    assert claim["id"] == "CLM-2026-W003"
    assert stored["CLM-2026-W003"]["id"] == "CLM-2026-W003"
