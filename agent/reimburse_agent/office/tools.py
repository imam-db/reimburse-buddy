"""Mock office tools: send_email, create_calendar_event, create_task.

They only append to a store: an in-memory dict (optionally JSON-file-backed) or, when the plan state lives
in Postgres, one `mock_office_rows` row per action. Nothing is sent anywhere. The plan graph calls them from
its executor node only, after the audit log holds a matching approval.

Both backends are idempotent per `action_id`: a resumed run that replays an approved action gets the row it
already produced, marked `duplicate: True`, and the store keeps one row.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .directory import Directory


class PostgresOfficeRows:
    """`mock_office_rows` (agent/migrations/006_plan_state.sql), one row per (run_id, store, action_id).

    The insert carries the id, so a replay cannot consume a second number: `INSERT ... ON CONFLICT DO
    NOTHING` either writes the row or leaves the first one in place, and the caller then reads it back with
    `duplicate: True`. Same result shape as the file backend, including the `OUT-0001` style id.
    """

    def __init__(self, pool):
        self.pool = pool

    def add(self, run_id: str, store: str, prefix: str, action_id: str, row: dict) -> dict:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mock_office_rows WHERE run_id = %s AND store = %s",
                        (run_id, store))
            n = cur.fetchone()[0] + 1
            full = {"id": f"{prefix}-{n:04d}", "action_id": action_id, **row}
            cur.execute(
                "INSERT INTO mock_office_rows (run_id, store, action_id, row) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (run_id, store, action_id) DO NOTHING "
                "RETURNING row",
                (run_id, store, action_id, json.dumps(full, ensure_ascii=False)))
            if cur.fetchone() is not None:
                return full
            cur.execute("SELECT row FROM mock_office_rows WHERE run_id = %s AND store = %s AND action_id = %s",
                        (run_id, store, action_id))
            return {**_decode(cur.fetchone()[0]), "duplicate": True}

    def count(self, run_id: str) -> int:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM mock_office_rows WHERE run_id = %s", (run_id,))
            return cur.fetchone()[0]


def _decode(value) -> dict:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


class MockOffice:
    """One run's mock office: `rows` (Postgres) or a JSON file, never both."""

    def __init__(self, directory: Directory, path: str | Path | None = None, *, run_id: str | None = None,
                 rows: PostgresOfficeRows | None = None):
        self.directory = directory
        self.path = Path(path) if path else None
        self.run_id = run_id
        self.rows = rows
        self._lock = threading.Lock()
        self.stores: dict[str, list[dict]] = {"outbox": [], "calendar": [], "tasks": []}
        if self.path and self.path.exists():
            self.stores.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _add(self, store: str, prefix: str, action_id: str, row: dict) -> dict:
        if self.rows is not None:
            return self.rows.add(self.run_id, store, prefix, action_id, row)
        with self._lock:
            for r in self.stores[store]:
                if r["action_id"] == action_id:  # idempotent per action_id
                    return {**r, "duplicate": True}
            row = {"id": f"{prefix}-{len(self.stores[store]) + 1:04d}", "action_id": action_id, **row}
            self.stores[store].append(row)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.stores, ensure_ascii=False, indent=1), encoding="utf-8")
            return row

    def send_email(self, action_id: str, to: list[str], subject: str, body: str) -> dict:
        addrs = [self.directory.get(t).email for t in to]
        return self._add("outbox", "OUT", action_id, {"to": to, "to_emails": addrs, "subject": subject, "body": body})

    def create_calendar_event(self, action_id: str, attendees: list[str], title: str, start: str,
                              duration_min: int, body: str = "") -> dict:
        return self._add("calendar", "CAL", action_id, {"attendees": attendees, "title": title, "start": start,
                                                         "duration_min": duration_min, "body": body})

    def create_task(self, action_id: str, assignee: str, title: str, body: str = "") -> dict:
        return self._add("tasks", "TSK", action_id, {"assignee": assignee, "title": title, "body": body})

    def call(self, tool: str, action_id: str, args: dict) -> dict:
        fn = {"send_email": self.send_email, "create_calendar_event": self.create_calendar_event,
              "create_task": self.create_task}[tool]
        return fn(action_id, **args)

    def count(self) -> int:
        if self.rows is not None:
            return self.rows.count(self.run_id)
        return sum(len(v) for v in self.stores.values())
