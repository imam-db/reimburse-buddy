"""Where this process keeps plan state: one pool, one store, one checkpointer.

The request intake needs three things to survive a restart: the plan store (plan, request, decisions), the
mock office rows, and the plan graph's checkpointer. With `REIMBURSE_PLAN_BACKEND=postgres` all three live
in the same Neon database and share one small connection pool, so a suspended endpoint costs one reconnect
instead of three.

The pool is built with `check=ConnectionPool.check_connection` (a dead socket is replaced when it is taken
from the pool) and `prepare_threshold=None` (no server-side prepared statements, safe behind a pooler).

Outside production the default backend is `file`: the store is a directory and the checkpointer is
`MemorySaver`, exactly as before this change.
"""

from __future__ import annotations

import sys
import threading

from .agent_bridge import AGENT_DIR

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from reimburse_agent.office import store as S  # noqa: E402
from reimburse_agent.office.tools import PostgresOfficeRows  # noqa: E402

CHECKPOINT_TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")
_STATE: dict = {}
_LOCK = threading.RLock()


def is_postgres() -> bool:
    return S.backend_name() == "postgres"


def pool():
    """The process pool, or None for the file backend."""
    if not is_postgres():
        return None
    dsn = S.state_dsn()
    if not dsn:
        raise S.StoreConfigError(
            "REIMBURSE_PLAN_BACKEND=postgres needs REIMBURSE_STATE_DATABASE_URL (or "
            "REIMBURSE_AUDIT_DATABASE_URL). Without it no plan, decision or checkpoint could be stored, so "
            "the web app refuses to start. See the README env table.")
    with _LOCK:
        if _STATE.get("dsn") != dsn:
            old = _STATE.get("pool")
            _STATE["pool"] = S.make_pool(dsn)
            _STATE["dsn"] = dsn
            _STATE.pop("saver", None)
            if old is not None:
                old.close()
        return _STATE["pool"]


def store() -> S.PlanStore:
    if not is_postgres():
        return S.FilePlanStore()
    return S.PostgresPlanStore(pool())


def office_rows():
    """The mock office's row store, or None when the office writes JSON files."""
    return PostgresOfficeRows(pool()) if is_postgres() else None


def checkpointer():
    """`PostgresSaver` on the shared pool, or None so the caller keeps `MemorySaver`."""
    if not is_postgres():
        return None
    p = pool()
    with _LOCK:
        if _STATE.get("saver_key") != id(p):
            from langgraph.checkpoint.postgres import PostgresSaver

            _STATE["saver"] = PostgresSaver(p)
            _STATE["saver_key"] = id(p)
        return _STATE["saver"]


def reset() -> None:
    """Forget the pool and the checkpointer (tests, Reset demo)."""
    with _LOCK:
        old = _STATE.get("pool")
        _STATE.clear()
    if old is not None:
        try:
            old.close()
        except Exception:  # noqa: BLE001 - closing a dead pool is not an error worth raising
            pass


def _missing(p, tables) -> list[str]:
    missing = []
    for table in tables:
        try:
            with p.connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM {table} LIMIT 0")
        except Exception:  # noqa: BLE001 - a missing table and a missing grant both mean "unusable"
            missing.append(table)
    return missing


def startup_check() -> None:
    """Fail startup when the postgres backend cannot work, with a message that says what to run.

    Runs after the audit, auth and spend checks and before the expiry sweep (see app.py). A silent fallback
    to the file store would lose the very state this backend exists to keep, so there is none."""
    if not is_postgres():
        return
    p = pool()  # raises StoreConfigError when the DSN is missing
    try:
        with p.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - the class is enough, the DSN must not be printed
        raise S.StoreConfigError(
            "The plan-state database is unreachable "
            f"({type(exc).__name__}), so no plan could be stored or resumed. Check "
            "REIMBURSE_STATE_DATABASE_URL.") from exc

    missing = _missing(p, S.PLAN_TABLES)
    if missing:
        raise S.StoreConfigError(
            "The plan-state tables are missing: " + ", ".join(missing) + ". Apply "
            "agent/migrations/006_plan_state.sql as the owner role.")
    missing = _missing(p, CHECKPOINT_TABLES)
    if missing:
        raise S.StoreConfigError(
            "The plan-graph checkpoint tables are missing: " + ", ".join(missing) + ". Run "
            "`python -m reimburse_agent.office.checkpoint_setup` as the owner role.")
