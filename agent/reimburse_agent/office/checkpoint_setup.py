"""Create the plan-graph checkpoint tables and let the app role use them.

Run once per database, as the OWNER role, before the web app starts with `REIMBURSE_PLAN_BACKEND=postgres`::

    NEON_OWNER_DSN=... uv run python -m reimburse_agent.office.checkpoint_setup

langgraph owns the shape of these tables, so they are created by `PostgresSaver.setup()` and not by a
migration in agent/migrations. This command then grants `reimburse_app` SELECT, INSERT, UPDATE and DELETE on
the checkpoint tables *only*: the graph rewrites a checkpoint on every step and deletes one when a thread is
pruned, while `audit_events` and `llm_spend` stay append-only.

The DSN is never printed: the owner DSN goes straight into psycopg, and the target is shown masked.
"""

from __future__ import annotations

import os
import sys

from ..audit_selftest import mask_dsn

APP_ROLE = "reimburse_app"
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


def checkpoint_tables(cur) -> list[str]:
    """The tables langgraph created (everything `checkpoint*` in the public schema)."""
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename LIKE 'checkpoint%' ORDER BY tablename")
    return [row[0] for row in cur.fetchall()]


def main() -> int:
    dsn = os.environ.get("NEON_OWNER_DSN", "").strip()
    if not dsn:
        print("FAIL NEON_OWNER_DSN is not set: run this as the owner role (migrations only).")
        return 2
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - the web group installs both
        print(f"FAIL missing dependency: {exc}")
        return 2

    print(f"target {mask_dsn(dsn)}")
    try:
        conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(f"FAIL cannot connect: {type(exc).__name__}")
        return 1

    with conn:
        with conn.cursor() as cur:
            try:
                PostgresSaver(conn).setup()
                print("PASS checkpoint tables created (PostgresSaver.setup)")
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL PostgresSaver.setup: {type(exc).__name__}: {exc}")
                return 1

            tables = checkpoint_tables(cur)
            if not tables:
                print("FAIL no checkpoint table was created")
                return 1

            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            if cur.fetchone() is None:
                print(f"FAIL role {APP_ROLE} does not exist: apply agent/migrations/001_audit_log.sql first")
                return 1

            for table in tables:
                cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}")
                            .format(sql.Identifier(table), sql.Identifier(APP_ROLE)))
            print(f"PASS granted {', '.join(PRIVILEGES)} on {len(tables)} checkpoint table(s) to {APP_ROLE}")

            bad = []
            for table in tables:
                for privilege in PRIVILEGES:
                    cur.execute("SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, privilege))
                    if not cur.fetchone()[0]:
                        bad.append(f"{table}:{privilege}")
            if bad:
                print(f"FAIL missing privilege: {', '.join(bad)}")
                return 1
            print(f"PASS {APP_ROLE} can read and write every checkpoint table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
