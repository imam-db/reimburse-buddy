"""Contact directory: who someone is, where they sit and in which time zone.

Data: the official kickoff contacts when REIMBURSE_OFFICE_CONTACTS points at them, otherwise the synthetic
gap-filler in data/contacts.json. Lookups are deterministic; the model never resolves a person itself.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "contacts.json"
HONORIFICS = {"pak", "bu", "ibu", "bapak", "mas", "mbak", "sdr", "sdri", "mr", "mrs", "ms", "dr"}


@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    role: str
    email: str
    office: str
    tz: str

    def short(self) -> str:
        return f"{self.name} ({self.id}, {self.role}, {self.office}, {self.tz})"


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", text.lower()) if t not in HONORIFICS]


class Directory:
    def __init__(self, contacts: list[dict]):
        self._by_id = {c["id"]: Contact(**{k: c[k] for k in ("id", "name", "role", "email", "office", "tz")})
                       for c in contacts}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Directory":
        p = Path(path or os.environ.get("REIMBURSE_OFFICE_CONTACTS") or DEFAULT_PATH)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data["contacts"] if isinstance(data, dict) else data)

    def ids(self) -> set[str]:
        return set(self._by_id)

    def get(self, emp_id: str) -> Contact | None:
        return self._by_id.get(emp_id)

    def find(self, name: str) -> list[Contact]:
        """Every contact whose name contains all the query's name tokens (honorifics ignored), or whose ID
        equals the query. Sorted by ID, so the result (and any question built from it) is deterministic."""
        q = name.strip()
        if q.upper() in self._by_id:
            return [self._by_id[q.upper()]]
        want = _tokens(q)
        if not want:
            return []
        return sorted((c for c in self._by_id.values() if all(w in _tokens(c.name) for w in want)),
                      key=lambda c: c.id)
