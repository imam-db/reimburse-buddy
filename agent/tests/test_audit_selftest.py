"""Self-test contract: PostgresAuditLog disables prepared statements and the audit self-test treats only a
denied write as PASS, exits non-zero on a write that succeeds, and never prints the DSN password."""

import sys
import types

from reimburse_agent import audit as A
from reimburse_agent import audit_selftest as S

DSN = "postgresql://reimburse_app:s3cr3t-pass@ep-direct.neon.tech:5432/reimburse"


class DeniedError(Exception):
    sqlstate = "42501"


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=()):
        self._conn.executed.append((sql, params))
        if not self._conn.allow_writes:
            raise DeniedError("permission denied for table audit_events")


class FakeConn:
    def __init__(self, allow_writes: bool = False):
        self.allow_writes = allow_writes
        self.autocommit = True
        self.prepare_threshold = None
        self.executed: list[tuple[str, tuple]] = []
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1


class FakeResultLog:
    """The AuditLog contract in memory, with a fake connection for the write probes."""

    def __init__(self, conn: FakeConn):
        self._conn = conn
        self._rows: list[dict] = []
        self._keys: set[str] = set()

    def append(self, event):
        row = A.validate(event)
        if row["event_key"] in self._keys:
            return False
        self._keys.add(row["event_key"])
        self._rows.append(row)
        return True

    def list(self, run_id=None):
        return [dict(r) for r in self._rows if run_id is None or r["run_id"] == run_id]


def _run(capsys, conn: FakeConn, monkeypatch):
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", DSN)
    code = S.main([], make_log=lambda dsn: FakeResultLog(conn))
    return code, capsys.readouterr().out


def test_denied_update_passes(capsys, monkeypatch):
    conn = FakeConn(allow_writes=False)
    code, out = _run(capsys, conn, monkeypatch)
    assert code == 0
    assert "PASS UPDATE denied" in out
    assert "FAIL" not in out
    # All three write probes ran and were rolled back even though they were denied.
    assert len(conn.executed) == len(S.WRITE_ATTEMPTS)
    assert conn.rollbacks == len(S.WRITE_ATTEMPTS)


def test_allowed_update_fails_and_exit_is_non_zero(capsys, monkeypatch):
    conn = FakeConn(allow_writes=True)
    code, out = _run(capsys, conn, monkeypatch)
    assert code != 0
    assert "FAIL UPDATE denied: statement succeeded" in out
    # A rolled-back transaction keeps even a misconfigured owner connection from damaging audit rows.
    assert conn.rollbacks == len(S.WRITE_ATTEMPTS)


def test_output_never_contains_the_dsn_password(capsys):
    def broken_connect(dsn):
        raise RuntimeError(f"cannot connect to {dsn}")

    code = S.main([], make_log=broken_connect, env={"REIMBURSE_AUDIT_DATABASE_URL": DSN})
    out = capsys.readouterr().out
    assert code != 0
    assert "s3cr3t-pass" not in out
    assert "reimburse_app:***@" in out


def test_write_probe_error_detail_is_redacted(capsys):
    class UnexpectedError(Exception):
        sqlstate = "XX000"

    class LeakyCursor(FakeCursor):
        def execute(self, sql, params=()):
            raise UnexpectedError(f"connection lost for {DSN}")

    class LeakyConn(FakeConn):
        def cursor(self):
            return LeakyCursor(self)

    code = S.main([], make_log=lambda dsn: FakeResultLog(LeakyConn()), env={"REIMBURSE_AUDIT_DATABASE_URL": DSN})
    out = capsys.readouterr().out
    assert code != 0
    assert "FAIL TRUNCATE denied: unexpected UnexpectedError" in out
    assert "s3cr3t-pass" not in out


def test_missing_dsn_fails(capsys):
    code = S.main([], env={})
    out = capsys.readouterr().out
    assert code != 0
    assert "REIMBURSE_AUDIT_DATABASE_URL" in out


def test_postgres_audit_log_connects_without_prepared_statements(monkeypatch):
    recorded = {}

    class Conn:
        prepare_threshold = None

    def connect(dsn, **kwargs):
        recorded["dsn"] = dsn
        recorded.update(kwargs)
        return Conn()

    fake = types.ModuleType("psycopg")
    fake.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    log = A.PostgresAuditLog(DSN)
    assert isinstance(log._conn, Conn)
    assert recorded["dsn"] == DSN
    assert recorded["autocommit"] is True
    assert recorded["prepare_threshold"] is None
