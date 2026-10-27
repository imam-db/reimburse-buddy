"""Mock office tools: send_email, create_calendar_event, create_task.

They only append to in-memory (optionally JSON-file-backed) stores: nothing is sent anywhere. The plan
graph calls them from its executor node only, after the audit log holds a matching approval.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .directory import Directory


class MockOffice:
    def __init__(self, directory: Directory, path: str | Path | None = None):
        self.directory = directory
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.stores: dict[str, list[dict]] = {"outbox": [], "calendar": [], "tasks": []}
        if self.path and self.path.exists():
            self.stores.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _add(self, store: str, prefix: str, action_id: str, row: dict) -> dict:
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
        return sum(len(v) for v in self.stores.values())
