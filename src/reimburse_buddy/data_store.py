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
            text = " ".join([m["subject"], m["body"], m["from"], json.dumps(m["attachments"])]).lower()
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
