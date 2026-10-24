"""Read-only access to the synthetic JSON data in ./data (or $REIMBURSE_BUDDY_DATA)."""

from __future__ import annotations

import json
import os
from functools import cached_property
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class DataStore:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or os.environ.get("REIMBURSE_BUDDY_DATA") or DEFAULT_DATA_DIR)

    def _load(self, name: str) -> Any:
        with open(self.data_dir / f"{name}.json", encoding="utf-8") as f:
            return json.load(f)

    @cached_property
    def employees(self) -> list[dict]:
        return self._load("employees")

    @cached_property
    def policy(self) -> dict:
        return self._load("policy")

    @cached_property
    def claims(self) -> list[dict]:
        return self._load("claims")

    @cached_property
    def inbox(self) -> list[dict]:
        return self._load("inbox")

    @cached_property
    def calendar(self) -> list[dict]:
        return self._load("calendar")

    @cached_property
    def memos(self) -> list[dict]:
        return self._load("memos")

    @cached_property
    def replies(self) -> list[dict]:
        path = self.data_dir / "replies.json"
        return self._load("replies") if path.exists() else []

    # --- in-memory state for mock write tools (reset per server process) ----
    @cached_property
    def outbox(self) -> list[dict]:
        return []

    @cached_property
    def approval_requests(self) -> list[dict]:
        return []

    @cached_property
    def status_log(self) -> list[dict]:
        return []

    # --- lookups -------------------------------------------------------
    def get_claim(self, claim_id: str) -> dict:
        for c in self.claims:
            if c["id"] == claim_id:
                return c
        raise KeyError(f"Unknown claim: {claim_id}")

    def get_employee(self, employee_id: str) -> dict:
        for e in self.employees:
            if e["id"] == employee_id:
                return e
        raise KeyError(f"Unknown employee: {employee_id}")

    def find_evidence(self, evidence_id: str) -> tuple[str, dict] | None:
        for kind, rows in (("inbox", self.inbox), ("calendar", self.calendar), ("memo", self.memos)):
            for r in rows:
                if r["id"] == evidence_id:
                    return kind, r
        return None

    # --- mock office tools --------------------------------------------
    def search_inbox(self, employee_id: str, query: str = "") -> list[dict]:
        terms = [t.lower() for t in query.split() if t]
        out = []
        for m in self.inbox:
            if m["owner_id"] != employee_id:
                continue
            # Like a real mail search: subject, body, sender and attachment file names only.
            names = [a.get("name", "") for a in m["attachments"]]
            text = " ".join([m["subject"], m["body"], m["from"], *names]).lower()
            if all(t in text for t in terms):
                out.append(m)
        return out

    def get_calendar(self, employee_id: str, date_from: str, date_to: str) -> list[dict]:
        return [e for e in self.calendar if e["owner_id"] == employee_id and date_from <= e["date"] <= date_to]

    def find_approval_memo(self, employee_id: str, city: str | None = None, date: str | None = None) -> list[dict]:
        out = []
        for m in self.memos:
            if employee_id not in m["employee_ids"]:
                continue
            if city and m["city"].lower() != city.lower():
                continue
            if date and not (m["valid_from"] <= date <= m["valid_to"]):
                continue
            out.append(m)
        return out

    # --- mock write tools ---------------------------------------------
    CLAIM_STATUSES = ("submitted", "on_hold", "approved", "partially_approved", "rejected")

    def send_message(self, to: str, body: str, subject: str = "", claim_id: str = "") -> dict:
        """Put a message in the outbox. Matching scripted replies (data/replies.json) are
        delivered at once into the employee's mailbox, so search_inbox can find them."""
        self.get_employee(to)
        msg = {"id": f"OUT-{len(self.outbox) + 1:03d}", "to": to, "subject": subject,
               "body": body, "claim_id": claim_id}
        self.outbox.append(msg)
        text = f"{subject} {body} {claim_id}".lower()
        delivered = []
        for r in self.replies:
            if r.get("_delivered") or r["to"] != to:
                continue
            if r.get("claim_id") and r["claim_id"] != claim_id:
                continue
            if any(k.lower() in text for k in r["keywords_any"]):
                r["_delivered"] = True
                self.inbox.append(dict(r["reply"], in_reply_to=msg["id"]))
                delivered.append(r["reply"]["id"])
        return {"status": "sent", "message_id": msg["id"], "replies_received": len(delivered)}

    def request_approval(self, manager_id: str, claim_id: str, item_id: str, reason: str = "") -> dict:
        claim = self.get_claim(claim_id)
        if item_id not in {i["id"] for i in claim["items"]}:
            raise KeyError(f"Unknown item {item_id} in {claim_id}")
        expected = self.get_employee(claim["employee_id"])["manager_id"]
        if manager_id != expected:
            raise ValueError(f"{manager_id} is not the manager of {claim['employee_id']} (expected {expected})")
        req = {"id": f"APR-{len(self.approval_requests) + 1:03d}", "manager_id": manager_id,
               "claim_id": claim_id, "item_id": item_id, "reason": reason, "status": "pending"}
        self.approval_requests.append(req)
        return req

    def update_claim_status(self, claim_id: str, status: str, note: str = "") -> dict:
        if status not in self.CLAIM_STATUSES:
            raise ValueError(f"status must be one of {self.CLAIM_STATUSES}")
        claim = self.get_claim(claim_id)
        entry = {"claim_id": claim_id, "from": claim["status"], "to": status, "note": note}
        claim["status"] = status
        self.status_log.append(entry)
        return entry
