"""Read-only access to the synthetic JSON data in ./data (or $REIMBURSE_BUDDY_DATA)."""

from __future__ import annotations

import json
import os
import re
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

    def reset(self) -> None:
        """Drop all in-memory state (delivered replies, outbox, approvals, status changes) and reload data."""
        for name in ("employees", "policy", "claims", "inbox", "calendar", "memos", "replies",
                     "outbox", "approval_requests", "status_log"):
            self.__dict__.pop(name, None)

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
    STOPWORDS = {"the", "a", "an", "for", "of", "to", "and", "or", "in", "on", "at", "my", "is", "please", "send",
                 "mohon", "kirim", "yang", "dan", "di", "ke", "untuk", "dari", "dengan"}

    @staticmethod
    def _compact(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def search_reimburse_mailbox(self, employee_id: str, query: str = "", limit: int = 5) -> list[dict]:
        """Search the shared reimburse@ mailbox, limited to messages an employee forwarded or sent
        about their own claims. No personal inbox is ever read. Ranked OR search over subject (weight 2), body, sender and attachment file names (weight 1);
        tokens containing digits (flight or invoice numbers) weigh 3x.
        Case-insensitive; "QZ7520" and "QZ 7520" match the same text. Returns short summaries."""
        tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in self.STOPWORDS]
        joined = self._compact(query)
        scored = []
        for m in self.inbox:
            if m["owner_id"] != employee_id:
                continue
            names = [a.get("name", "") for a in m["attachments"]]
            subject = m["subject"].lower()
            rest = " ".join([m["body"], m["from"], *names]).lower()
            if tokens:
                cs, cr = self._compact(subject), self._compact(rest)
                score = 0
                for t in set(tokens):
                    w = 3 if any(ch.isdigit() for ch in t) else 1  # identifiers (flight, invoice no.) weigh more
                    score += w * (2 if t in subject or t in cs else 1 if t in rest or t in cr else 0)
                if len(tokens) > 1 and joined and joined in self._compact(subject + " " + rest):
                    score += 2  # whole query appears, ignoring spaces and punctuation
            else:
                score = 1
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: x[1]["date"], reverse=True)  # newest first on ties
        scored.sort(key=lambda x: -x[0])
        return [{"id": m["id"], "date": m["date"], "from": m["from"], "subject": m["subject"],
                 "snippet": m["body"][:160], "attachments": [a.get("name", "") for a in m["attachments"]],
                 "score": score} for score, m in scored[:limit]]

    search_inbox = search_reimburse_mailbox  # backwards-compatible alias

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
        """Put a message in the outbox. Matching scripted replies (data/replies.json) are the
        employee's answer, delivered at once to the reimburse@ mailbox."""
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
