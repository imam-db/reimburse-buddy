"""Pending plans and reviewer decisions on disk, shared by the agent process and the web app.

Layout: <root>/<run_id>/plan.json and <root>/<run_id>/decisions-v<version>.json. Root comes from
REIMBURSE_PLAN_STORE (default: agent/.plans, git-ignored).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / ".plans"
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class PlanStore:
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

    def runs(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/plan.json")) if self.root.exists() else []
