"""Pending plans and reviewer decisions, on disk or in Postgres.

The web app and the agent process share one store. Two backends implement the same seven methods:

* `FilePlanStore` — `<root>/<run_id>/plan.json`, `request.json`, `decisions-v<version>.json`. Root comes
  from `REIMBURSE_PLAN_STORE` (default: agent/.plans, git-ignored). This is the dev and test backend.
* `PostgresPlanStore` — one row per fact (agent/migrations/006_plan_state.sql), so a plan that is waiting
  on an approval survives a web restart and is visible to a second instance.

`open_store()` picks one: `REIMBURSE_PLAN_BACKEND=file|postgres`, defaulting to postgres when
`APP_ENV=prod` and to file everywhere else. The DSN is `REIMBURSE_STATE_DATABASE_URL`, falling back to
`REIMBURSE_AUDIT_DATABASE_URL` (one Neon database serves audit, spend and plan state).

Nothing here imports psycopg at module level: the office layer is standard-library only, so the web app can
import it without the postgres extra installed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / ".plans"
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
PRODUCTION_ENVS = frozenset({"prod", "production"})
PLAN_TABLES = ("plan_requests", "plan_versions", "plan_decisions", "mock_office_rows")


class StoreConfigError(RuntimeError):
    """The chosen backend cannot be used (no DSN, a missing table, an unknown backend name)."""


@runtime_checkable
class PlanStore(Protocol):
    """What the plan graph, the web app and the external driver need from a store."""

    def save_plan(self, plan: dict) -> None: ...
    def load_plan(self, run_id: str) -> dict | None: ...
    def save_decisions(self, run_id: str, version: int, decision: dict) -> None: ...
    def load_decisions(self, run_id: str, version: int) -> dict | None: ...
    def save_request(self, run_id: str, request: dict) -> None: ...
    def load_request(self, run_id: str) -> dict | None: ...
    def runs(self) -> list[str]: ...


class FilePlanStore:
    """One directory per run, one JSON file per fact. Unchanged behaviour, including the file names."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("REIMBURSE_PLAN_STORE") or DEFAULT_ROOT)

    def _dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id or ""):
            raise ValueError(f"bad run_id {run_id!r}")
        return self.root / run_id

    def _write(self, p: Path, data) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def save_plan(self, plan: dict) -> None:
        self._write(self._dir(plan["run_id"]) / "plan.json", plan)

    def load_plan(self, run_id: str) -> dict | None:
        p = self._dir(run_id) / "plan.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_decisions(self, run_id: str, version: int, decision: dict) -> None:
        self._write(self._dir(run_id) / f"decisions-v{version}.json", decision)

    def load_decisions(self, run_id: str, version: int) -> dict | None:
        p = self._dir(run_id) / f"decisions-v{version}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_request(self, run_id: str, request: dict) -> None:
        """The original intake request, so a new run can replay it (Jalankan ulang)."""
        self._write(self._dir(run_id) / "request.json", request)

    def load_request(self, run_id: str) -> dict | None:
        p = self._dir(run_id) / "request.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def runs(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/plan.json")) if self.root.exists() else []


class PostgresPlanStore:
    """Same contract on Postgres, sharing the pool with the checkpointer.

    `load_plan(run_id)` is the newest version: a re-plan adds a row to plan_versions instead of overwriting,
    so the history of a run stays readable. `save_plan` upserts the row of that plan's own version, which is
    what the web layer's submitted_by stamp needs.

    The pool is passed in, not built here: `make_pool()` is the only place that opens connections, and the
    plan graph's checkpointer uses the same pool, so one Neon endpoint sees one small set of connections.
    """

    def __init__(self, pool):
        self.pool = pool

    def save_plan(self, plan: dict) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_versions (run_id, version, plan, updated_at) "
                "VALUES (%s, %s, %s::jsonb, now()) "
                "ON CONFLICT (run_id, version) DO UPDATE SET plan = EXCLUDED.plan, updated_at = now()",
                (plan["run_id"], int(plan["version"]), json.dumps(plan, ensure_ascii=False)))

    def load_plan(self, run_id: str) -> dict | None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT plan FROM plan_versions WHERE run_id = %s ORDER BY version DESC LIMIT 1",
                        (run_id,))
            row = cur.fetchone()
        return None if row is None else _json(row[0])

    def save_decisions(self, run_id: str, version: int, decision: dict) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_decisions (run_id, version, decision) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (run_id, version) DO UPDATE SET decision = EXCLUDED.decision",
                (run_id, int(version), json.dumps(decision, ensure_ascii=False)))

    def load_decisions(self, run_id: str, version: int) -> dict | None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT decision FROM plan_decisions WHERE run_id = %s AND version = %s",
                        (run_id, int(version)))
            row = cur.fetchone()
        return None if row is None else _json(row[0])

    def save_request(self, run_id: str, request: dict) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_requests (run_id, request) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (run_id) DO UPDATE SET request = EXCLUDED.request",
                (run_id, json.dumps(request, ensure_ascii=False)))

    def load_request(self, run_id: str) -> dict | None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT request FROM plan_requests WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        return None if row is None else _json(row[0])

    def runs(self) -> list[str]:
        """Runs with at least one staged plan (a run still on a who-question has no plan yet)."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT run_id FROM plan_versions ORDER BY run_id")
            return [r[0] for r in cur.fetchall()]

    def missing_tables(self) -> list[str]:
        """Which plan-state tables this role cannot read (empty when the store is usable).

        One `SELECT 1 ... LIMIT 0` per table: that catches a missing table and a missing grant in one
        probe, which is what startup needs to know."""
        missing = []
        for table in PLAN_TABLES:
            try:
                with self.pool.connection() as conn, conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM {table} LIMIT 0")
            except Exception:  # noqa: BLE001 - any failure here means "cannot use this table"
                missing.append(table)
        return missing


def _json(value):
    """psycopg returns jsonb as dict; a text column would need decoding."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


# --- backend selection ---------------------------------------------------------------------------
def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in PRODUCTION_ENVS


def state_dsn() -> str:
    """The DSN of the database that holds plan state and checkpoints ("" when unset)."""
    return (os.environ.get("REIMBURSE_STATE_DATABASE_URL", "").strip()
            or os.environ.get("REIMBURSE_AUDIT_DATABASE_URL", "").strip())


def backend_name() -> str:
    """`file` or `postgres`. An explicit value wins; otherwise prod is postgres and everything else file."""
    raw = os.environ.get("REIMBURSE_PLAN_BACKEND", "").strip().lower()
    if raw in ("file", "postgres"):
        return raw
    if raw:
        raise StoreConfigError(
            f"REIMBURSE_PLAN_BACKEND={raw!r} is not a backend. Use 'file' or 'postgres'.")
    return "postgres" if is_production() else "file"


def make_pool(dsn: str):
    """One small pool for the plan store and the checkpointer.

    `check=ConnectionPool.check_connection` pings a connection when it is taken from the pool, so a Neon
    endpoint that suspended while idle is replaced instead of handing out a dead socket.
    `prepare_threshold=None` keeps psycopg from creating server-side prepared statements, which is what
    makes a pooler (Neon's `-pooler` endpoint) safe; audit.py and spend.py connect the same way.
    """
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise StoreConfigError(
            "REIMBURSE_PLAN_BACKEND=postgres needs psycopg-pool: install the root 'web' group "
            "(uv sync) or `pip install psycopg-pool`.") from exc

    return ConnectionPool(dsn, min_size=1, max_size=4, timeout=10.0, open=True,
                          check=ConnectionPool.check_connection,
                          kwargs={"autocommit": True, "prepare_threshold": None},
                          name="reimburse-plan-state")


def open_store() -> PlanStore:
    """The store the backend setting asks for. Raises StoreConfigError when it cannot exist."""
    backend = backend_name()
    if backend == "file":
        return FilePlanStore()
    dsn = state_dsn()
    if not dsn:
        raise StoreConfigError(
            "REIMBURSE_PLAN_BACKEND=postgres needs REIMBURSE_STATE_DATABASE_URL (or "
            "REIMBURSE_AUDIT_DATABASE_URL): without it no plan, decision or checkpoint could be stored.")
    return PostgresPlanStore(make_pool(dsn))
