"""PostgresAuditLog reconnects once after Neon closes an idle connection (fake psycopg, no database)."""
import sys
import types

import pytest

import reimburse_agent.audit as A

DSN = "postgresql://user:pw@localhost/db"


class OpErr(Exception):
    pass


class IfErr(Exception):
    pass


class Cur:
    def __init__(self, conn):
        self.conn, self.rowcount, self._row = conn, 0, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        c = self.conn
        if c.fail_next:
            c.fail_next -= 1
            raise OpErr("the connection is closed")
        if sql.startswith("INSERT"):
            c.inserts += 1
            self.rowcount = 0 if c.preexisting_id is not None else 1
        elif sql.startswith("SELECT id::text"):
            self._row = (c.preexisting_id,)
        else:
            self._rows = []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class Conn:
    def __init__(self, fail_next=0, preexisting_id=None):
        self.fail_next, self.preexisting_id, self.inserts, self.closed = fail_next, preexisting_id, 0, False

    def cursor(self):
        return Cur(self)

    def close(self):
        self.closed = True


def make_log(monkeypatch, conns):
    it = iter(conns)
    fake = types.ModuleType("psycopg")
    fake.OperationalError, fake.InterfaceError = OpErr, IfErr
    fake.connect = lambda dsn, **kw: next(it)
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return A.PostgresAuditLog(DSN)


def ev():
    return A.make_event("run-1", "staged_action", "a1", actor="agent", action_id="a1", args_hash="h", payload={})


def test_append_reconnects_and_returns_true(monkeypatch):
    new = Conn()
    log = make_log(monkeypatch, [Conn(fail_next=1), new])
    assert log.append(ev()) is True
    assert log._conn is new and new.inserts == 1


def test_closed_connection_is_replaced_before_use(monkeypatch):
    old, new = Conn(), Conn()
    log = make_log(monkeypatch, [old, new])
    old.closed = True
    assert log.append(ev()) is True
    assert log._conn is new


def test_retry_after_insert_already_landed_returns_true(monkeypatch):
    e = ev()
    log = make_log(monkeypatch, [Conn(fail_next=1), Conn(preexisting_id=e["id"])])
    assert log.append(e) is True


def test_real_duplicate_still_returns_false(monkeypatch):
    log = make_log(monkeypatch, [Conn(preexisting_id="other")])
    assert log.append(ev()) is False


def test_two_failures_raise(monkeypatch):
    log = make_log(monkeypatch, [Conn(fail_next=1), Conn(fail_next=1)])
    with pytest.raises(OpErr):
        log.append(ev())


def test_list_reconnects(monkeypatch):
    log = make_log(monkeypatch, [Conn(fail_next=1), Conn()])
    assert log.list() == []
