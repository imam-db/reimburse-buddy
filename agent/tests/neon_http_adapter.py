"""TEST-ONLY pytest plugin: a minimal psycopg stand-in that sends SQL over Neon's HTTPS /sql endpoint.

For boxes where outbound port 5432 is blocked. Not imported by app code; load it explicitly:
    DATABASE_URL=<app-role dsn> uv run pytest -p tests.neon_http_adapter -m postgres tests/test_audit_contract.py
It implements only what PostgresAuditLog uses: connect(dsn, autocommit=True), conn.cursor() as a context
manager, cur.execute(sql, params), cur.rowcount, cur.fetchall(). The real psycopg driver is NOT exercised.
"""
import datetime as _dt
import json
import re
import sys
import types
import urllib.error
import urllib.parse
import urllib.request

_TIMESTAMPTZ, _TIMESTAMP, _JSON, _JSONB = 1184, 1114, 114, 3802


class Error(Exception):
    pass


def _parse(value, oid):
    if value is None:
        return None
    if oid in (_JSON, _JSONB):
        return json.loads(value)
    if oid in (_TIMESTAMPTZ, _TIMESTAMP):
        v = value.replace(" ", "T", 1)
        v = re.sub(r"([+-]\d\d)$", r"\1:00", v)
        return _dt.datetime.fromisoformat(v)
    return value


def _placeholders(sql):
    n = 0

    def sub(_):
        nonlocal n
        n += 1
        return f"${n}"
    return re.sub(r"%s", sub, sql)


class Cursor:
    def __init__(self, conn):
        self._conn, self.rowcount, self._rows = conn, -1, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        params = [None if p is None else p if isinstance(p, str) else str(p) for p in params]
        res = self._conn._post({"query": _placeholders(sql), "params": params})
        fields = res.get("fields") or []
        self.rowcount = res.get("rowCount") if res.get("rowCount") is not None else -1
        self._rows = [tuple(_parse(r.get(f["name"]), f["dataTypeID"]) for f in fields) for r in res.get("rows", [])]

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class Connection:
    def __init__(self, dsn, autocommit=True):
        if not autocommit:
            raise Error("neon_http_adapter supports autocommit only")
        self._dsn = dsn
        self._host = urllib.parse.urlparse(dsn).hostname

    def _post(self, body):
        req = urllib.request.Request(
            f"https://{self._host}/sql", data=json.dumps(body).encode(),
            headers={"Neon-Connection-String": self._dsn, "Content-Type": "application/json",
                     "Neon-Raw-Text-Output": "true", "Neon-Array-Mode": "false"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message", "")
            except Exception:
                msg = ""
            raise Error(f"neon http {e.code}: {msg}") from None  # never echo the DSN

    def cursor(self):
        return Cursor(self)

    def close(self):
        pass


def connect(dsn, autocommit=False, **_):
    return Connection(dsn, autocommit=autocommit)


def pytest_configure(config):
    mod = types.ModuleType("psycopg")
    mod.connect, mod.Error = connect, Error
    sys.modules["psycopg"] = mod
