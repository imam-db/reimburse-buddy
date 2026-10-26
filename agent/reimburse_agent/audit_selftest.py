"""Post-deploy self-test for the Postgres audit log, run as the application role.

After a deploy, point ``REIMBURSE_AUDIT_DATABASE_URL`` at the audit database (Neon direct/unpooled
endpoint, role with INSERT + SELECT only) and run::

    uv run python -m reimburse_agent.audit_selftest

Every check prints one PASS/FAIL line and the process exits non-zero when anything fails. The DSN and
its password are never printed. Reuses the contract exercised in ``agent/tests/test_audit_contract.py``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from . import audit as A

RUN_PREFIX = "selftest-"
SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{'PASS' if self.ok else 'FAIL'} {self.name}{suffix}"


# --- DSN redaction -------------------------------------------------------------------------------------
def mask_dsn(dsn: str) -> str:
    """The DSN with its password (and userinfo) replaced, safe to print."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "postgresql://***"
    if not parts.scheme or "@" not in parts.netloc:
        return dsn
    userinfo, _, host = parts.netloc.rpartition("@")
    user = userinfo.partition(":")[0]
    masked = f"{user}:***@{host}" if user else f"***@{host}"
    return urlunsplit((parts.scheme, masked, parts.path, parts.query, parts.fragment))


def _dsn_secrets(dsn: str) -> set[str]:
    secrets: set[str] = set()
    try:
        password = urlsplit(dsn).password
    except ValueError:
        return secrets
    if password:
        secrets.add(password)
        secrets.add(unquote(password))
    return secrets


def redact(text: object, dsn: str) -> str:
    """Replace the full DSN and its password anywhere in ``text`` (never leak a credential)."""
    s = str(text)
    if not dsn:
        return s
    s = s.replace(dsn, mask_dsn(dsn))
    for secret in sorted(_dsn_secrets(dsn), key=len, reverse=True):
        if secret:
            s = s.replace(secret, "***")
    return s


# --- checks ----------------------------------------------------------------------------------------------
def _is_permission_denied(exc: BaseException) -> bool:
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate is not None:
        return str(sqlstate) == SQLSTATE_INSUFFICIENT_PRIVILEGE
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return ("permission" in name or "insufficientprivilege" in name
            or "permission denied" in text or "insufficient privilege" in text or "must be owner" in text)


WRITE_ATTEMPTS = (
    ("UPDATE denied", "UPDATE audit_events SET actor = actor WHERE run_id = %s", True),
    ("DELETE denied", "DELETE FROM audit_events WHERE run_id = %s", True),
    ("TRUNCATE denied", "TRUNCATE audit_events", False),
)


def _attempt_denied(conn, sql: str, params: tuple = ()) -> tuple[bool, str]:
    """Run one write inside a transaction that is always rolled back.

    Returns ``(denied, detail)``. The rollback means even a wrongly privileged (owner/superuser)
    connection cannot change or truncate real audit rows; the check still FAILs because the write was
    allowed. PASS only for a permission denial (SQLSTATE 42501, or an equivalent permission message).
    """
    previous = getattr(conn, "autocommit", True)
    try:
        conn.autocommit = False
    except Exception:
        pass
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    except Exception as exc:
        if _is_permission_denied(exc):
            return True, f"{type(exc).__name__} (permission denied)"
        return False, f"unexpected {type(exc).__name__}: {exc}"
    else:
        return False, "statement succeeded (the app role must not be able to write)"
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.autocommit = previous
        except Exception:
            pass


def _check_prepare_threshold(conn) -> CheckResult:
    name = "prepare_threshold is None"
    if conn is None:
        return CheckResult(name, False, "no connection")
    if not hasattr(conn, "prepare_threshold"):
        return CheckResult(name, False, "connection exposes no prepare_threshold")
    value = conn.prepare_threshold
    return CheckResult(name, value is None, "" if value is None else f"got {value!r}")


def _check_roundtrip(log, run_id: str) -> CheckResult:
    name = "append + list roundtrip"
    events = [A.make_event(run_id, "request", "r1"), A.make_event(run_id, "plan", "p1")]
    appended = [log.append(row) for row in events]
    if appended != [True, True]:
        return CheckResult(name, False, f"append returned {appended!r}")
    types = [row["event_type"] for row in log.list(run_id)]
    if types != ["request", "plan"]:
        return CheckResult(name, False, f"listed {types!r}")
    return CheckResult(name, True, f"2 events under {run_id}")


def _check_duplicate(log, run_id: str) -> CheckResult:
    name = "duplicate event_key returns False"
    event = A.make_event(run_id, "tool_call", "dup")
    first = log.append(event)
    second = log.append({**event, "id": str(uuid.uuid4())})
    rows = [row for row in log.list(run_id) if row["event_key"] == event["event_key"]]
    ok = first is True and second is False and len(rows) == 1
    return CheckResult(name, ok, "" if ok else f"first={first!r} second={second!r} rows={len(rows)}")


def _check_unmatched(log, run_id: str) -> CheckResult:
    name = "unmatched_executions is empty"
    run = f"{run_id}-exec"
    matched_hash = "selftest"
    log.append(A.make_event(run, "approval", "a1", actor="human:selftest", action_id="a1", args_hash=matched_hash))
    log.append(A.make_event(run, "execution", "a1", actor="system", action_id="a1", args_hash=matched_hash))
    bad = A.unmatched_executions(log.list(run))
    return CheckResult(name, not bad, "" if not bad else f"{len(bad)} execution(s) without approval")


def run_checks(dsn: str, *, make_log=None) -> list[CheckResult]:
    """Run every contract check against ``dsn`` and return one result per check."""
    make = make_log or A.PostgresAuditLog
    run_id = RUN_PREFIX + uuid.uuid4().hex
    try:
        log = make(dsn)
    except Exception as exc:
        return [CheckResult("connect to Postgres", False, redact(f"{type(exc).__name__}: {exc}", dsn))]

    conn = getattr(log, "_conn", None)
    plan = (
        ("prepare_threshold is None", lambda: _check_prepare_threshold(conn)),
        ("append + list roundtrip", lambda: _check_roundtrip(log, run_id)),
        ("duplicate event_key returns False", lambda: _check_duplicate(log, run_id)),
    )
    results: list[CheckResult] = []
    for name, check in plan:
        try:
            result = check()
        except Exception as exc:
            result = CheckResult(name, False, f"{type(exc).__name__}: {exc}")
        results.append(CheckResult(result.name, result.ok, redact(result.detail, dsn)))

    for name, sql, uses_run_id in WRITE_ATTEMPTS:
        try:
            ok, detail = _attempt_denied(conn, sql, (run_id,) if uses_run_id else ())
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(CheckResult(name, ok, redact(detail, dsn)))

    try:
        result = _check_unmatched(log, run_id)
    except Exception as exc:
        result = CheckResult("unmatched_executions is empty", False, f"{type(exc).__name__}: {exc}")
    results.append(CheckResult(result.name, result.ok, redact(result.detail, dsn)))
    return results


def main(argv=None, *, make_log=None, env=None) -> int:
    """CLI entry point: 0 when every check passes, 1 on any FAIL, 2 on a bad configuration."""
    env = os.environ if env is None else env
    dsn = (env.get("REIMBURSE_AUDIT_DATABASE_URL") or "").strip()
    if not dsn:
        print("FAIL config: REIMBURSE_AUDIT_DATABASE_URL is not set")
        return 2
    if not dsn.lower().startswith(("postgres://", "postgresql://")):
        print(f"FAIL config: not a Postgres URL: {mask_dsn(dsn)}")
        return 2

    results = run_checks(dsn, make_log=make_log)
    for result in results:
        print(result.line())
    return 0 if results and all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
